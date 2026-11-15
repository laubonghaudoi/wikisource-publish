#!/usr/bin/env python3
"""Prepare, publish and verify one Markdown book on Chinese Wikisource."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import quote, unquote, urlparse

import requests
from PIL import Image

from md_wikitext import load_markdown, collect_images, convert, source_html, canonical_text, table_signatures, list_signatures
from mediawiki_client import MediaWikiClient, MediaWikiError, UploadWarning, AmbiguousWriteError

COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
SOURCE_API = 'https://zh.wikisource.org/w/api.php'
CREDENTIALS = Path.home() / '.config/wikisource-publish/credentials.json'


class PublicationError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def emit(event, **data):
    print(json.dumps({'event': event, **data}, ensure_ascii=False), flush=True)


def filename(value):
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*#\[\]{}]', '-', value).strip(' .')
    while len(value.encode('utf-8')) > 220:
        value = value[:-1]
    if not value:
        raise PublicationError('Empty file/title after normalization')
    return value


def text_parameter(value):
    return html.escape(str(value), quote=False).replace('|', '&#124;').replace('{', '&#123;').replace('}', '&#125;')


def publication_equal(left, right):
    # MediaWiki removes terminal newlines when saving wikitext. No source
    # characters, internal whitespace or Unicode spaces are normalized here.
    return left.rstrip('\n') == right.rstrip('\n')


def commons_name(value):
    parsed = urlparse(value)
    if parsed.scheme in ('http', 'https'):
        if parsed.hostname == 'upload.wikimedia.org' and '/wikipedia/commons/' in parsed.path and '/thumb/' not in parsed.path:
            return unquote(parsed.path.rsplit('/', 1)[-1]).replace('_', ' ')
        if parsed.hostname == 'commons.wikimedia.org' and '/wiki/File:' in parsed.path:
            return unquote(parsed.path.split('/wiki/File:', 1)[1]).replace('_', ' ')
        return None
    if value.startswith('File:'):
        return value[5:].replace('_', ' ')
    return None


def credentials(path):
    path = Path(path).expanduser()
    if not path.exists():
        raise PublicationError('Credentials are not configured. Run configure_credentials.py in your own terminal first.')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PublicationError('Credential file must be readable only by its owner (chmod 600).')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not data.get('username') or not data.get('password'):
        raise PublicationError('Credential file is missing username or password; no secret was printed.')
    return data


def clients(path=None):
    data = credentials(path) if path else {}
    return (MediaWikiClient(COMMONS_API, **data), MediaWikiClient(SOURCE_API, **data))


def checked_local(path, expected):
    path = Path(path)
    if not path.is_file() or digest(path) != expected:
        raise PublicationError(f'Input changed or disappeared since preparation: {path}. Prepare again.')


def file_record(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path), 'sha1': digest(path, 'sha1'), 'size': path.stat().st_size}


def resolve_pdf(value, work, commons, title, metadata):
    name = commons_name(value)
    if name:
        info = commons.file_info(name)
        if not info or info.get('mime') != 'application/pdf':
            raise PublicationError('The specified Commons PDF does not exist or is not a PDF.')
        return {'input': value, 'filename': info['name'], 'remote': info, 'sha1': info['sha1'], 'status': 'reused'}
    parsed = urlparse(value)
    if parsed.scheme in ('http', 'https'):
        path = work / 'source.pdf'
        response = requests.get(value, stream=True, timeout=(15, 60), headers={'User-Agent': 'WikisourcePublish/1.0 (user-directed book publishing)'})
        response.raise_for_status()
        with path.open('wb') as stream:
            for block in response.iter_content(1024 * 1024):
                stream.write(block)
    else:
        path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise PublicationError(f'PDF not found: {path}')
    with path.open('rb') as stream:
        if not stream.read(1024).lstrip().startswith(b'%PDF-'):
            raise PublicationError('Source is not a PDF (missing PDF signature).')
    result = file_record(path)
    requested = metadata.get('pdf_filename', title + ' - ' + result['sha1'][:10])
    if requested.lower().endswith('.pdf'):
        requested = requested[:-4]
    result.update(input=value, filename=filename(requested) + '.pdf', status='pending')
    matches = commons.find_files_by_sha1(result['sha1'])
    if matches:
        info = matches[0]
        result.update(filename=info['name'], remote=info, status='reused')
    return result


def make_header(job):
    meta = job['metadata']
    # Official/organizational documents can use the wiki's issuing-body field.
    # Keep author metadata separately for source attribution on file pages.
    fields = {'title': job['title']}
    fields['lawmaker' if meta.get('lawmaker') else 'author'] = meta.get('lawmaker') or meta['author']
    for key in ('year', 'month', 'day', 'edition', 'notes'):
        if meta.get(key) not in (None, ''):
            fields[key] = meta[key]
    header = '{{header\n' + ''.join(f' | {key} = {text_parameter(value)}\n' for key, value in fields.items()) + '}}'
    return header + '\n\n[[File:' + job['pdf']['filename'] + '|thumb|' + text_parameter(job['title']) + '原文掃描]]\n\n'


def read_pdf_page_map(path, targets, pdf):
    """Use an explicit, reviewed full-page map, never infer it from filenames."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    pages = data.get('image_pages') if isinstance(data, dict) else None
    if not isinstance(pages, dict) or not pages:
        raise PublicationError('PDF page map must contain a nonempty image_pages object.')
    if pdf.get('status') != 'reused' or not pdf.get('filename', '').lower().endswith('.pdf'):
        raise PublicationError('PDF page reuse requires an already existing Commons PDF.')
    if set(pages) - set(targets):
        raise PublicationError('PDF page map refers to images absent from the Markdown.')
    if any(type(page) is not int or page < 1 for page in pages.values()):
        raise PublicationError('PDF page numbers must be positive integers.')
    evidence = data.get('evidence', {})
    count = evidence.get('source_pdf_pages')
    if count is not None and (type(count) is not int or count < max(pages.values())):
        raise PublicationError('Mapped page exceeds the recorded source PDF page count.')
    if evidence.get('source_pdf'):
        source = Path(evidence['source_pdf'])
        if digest(source, 'sha1') != pdf['sha1']:
            raise PublicationError('PDF page-map evidence refers to a different source PDF.')
        if evidence.get('source_pdf_sha256'):
            checked_local(source, evidence['source_pdf_sha256'])
    return pages


def image_mapping(job):
    mapping = {}
    for im in job['images']:
        if im.get('source_kind') == 'pdf_page':
            page = im.get('pdf_page')
            if type(page) is not int or page < 1 or im.get('source_pdf_sha1') != job['pdf']['sha1']:
                raise PublicationError('Invalid PDF page mapping or changed source PDF hash.')
            mapping[im['target']] = {'filename': job['pdf']['filename'], 'page': page}
        else:
            mapping[im['target']] = im.get('commons_filename', im['requested_filename'])
    return mapping


def validate_pdf_embeds(parsed_html, job):
    """Check the actual PDF thumbnail page and order, including the cover."""
    expected = [im['page'] for im in job['image_order'] if 'page' in im]
    if not expected:
        return True
    pdf_name = job['pdf']['filename'].replace('_', ' ')
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.pages = []
        def handle_starttag(self, tag, attrs):
            if tag != 'img':
                return
            src = dict(attrs).get('src', '')
            parts = unquote(urlparse(src).path).split('/')
            for index, part in enumerate(parts[:-1]):
                if part.replace('_', ' ') == pdf_name:
                    match = re.match(r'page(\d+)-', parts[index + 1])
                    if match:
                        self.pages.append(int(match.group(1)))
    reader = Reader()
    reader.feed(parsed_html)
    return reader.pages == [1, *expected]


def render_job(job, work):
    checked_local(job['markdown']['path'], job['markdown']['sha256'])
    _, body = load_markdown(job['markdown']['path'])
    converted = convert(body, image_mapping(job))
    body_wiki = converted['wikitext']
    categories = '\n'.join(job.get('categories', []))
    # The source has historic glyph variants; do not let Chinese variant conversion rewrite them.
    whole = '__NOCONTENTCONVERT__\n' + make_header(job) + body_wiki.rstrip() + '\n\n' + job['metadata']['license_wikisource'].strip() + '\n'
    if categories:
        whole += '\n' + categories + '\n'
    whole = whole.rstrip('\n')
    (work / 'body.wikitext').write_text(body_wiki, encoding='utf-8')
    (work / 'publication.wikitext').write_text(whole, encoding='utf-8')
    (work / 'source-body.html').write_text(source_html(body), encoding='utf-8')
    json_write(work / 'expected-tables.json', converted['expected_tables'])
    job['statistics'] = converted['statistics']
    job['publication_sha256'] = hashlib.sha256(whole.encode()).hexdigest()
    job['body_sha256'] = hashlib.sha256(body_wiki.encode()).hexdigest()
    job['image_order'] = converted['images']
    json_write(work / 'job.json', job)
    return whole, body_wiki


def first_difference(a, b):
    end = min(len(a), len(b))
    for i in range(end):
        if a[i] != b[i]:
            return {'offset': i, 'expected': a[max(0, i-30):i+80], 'actual': b[max(0, i-30):i+80]}
    return {'offset': end, 'expected': a[max(0, end-30):end+80], 'actual': b[max(0, end-30):end+80]}


class StructureReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = {key: 0 for key in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li')}
        self.headings = []
        self.active_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.counts:
            self.counts[tag] += 1
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.active_heading = [tag, '']

    def handle_data(self, data):
        if self.active_heading is not None:
            self.active_heading[1] += data

    def handle_endtag(self, tag):
        if self.active_heading is not None and tag == self.active_heading[0]:
            self.active_heading[1] = re.sub(r'\s+', '', self.active_heading[1])
            self.headings.append(self.active_heading)
            self.active_heading = None


def document_structure(value):
    reader = StructureReader()
    reader.feed(value)
    return {'counts': reader.counts, 'headings': reader.headings}


def validate_body(job, work, wiki):
    text = (work / 'body.wikitext').read_text(encoding='utf-8')
    # Image files are verified separately. Removing embeds permits losslessness checks before uploading.
    text = re.sub(r'\[\[File:.*?\]\]', '', text, flags=re.S)
    parsed = wiki.parse('__NOCONTENTCONVERT__\n__NOTOC__\n' + text, job['title'])
    actual_html = parsed['html']
    (work / 'body-preview.html').write_text(actual_html, encoding='utf-8')
    expected = json.loads((work / 'expected-tables.json').read_text())
    actual = table_signatures(actual_html)
    table_ok = expected == actual
    if not table_ok:
        json_write(work / 'actual-tables.json', actual)
    expected_text = canonical_text((work / 'source-body.html').read_text())
    actual_text = canonical_text(actual_html)
    source_structure = document_structure((work / 'source-body.html').read_text())
    rendered_structure = document_structure(actual_html)
    lists_equal = list_signatures((work / 'source-body.html').read_text()) == list_signatures(actual_html)
    report = {'checked_utc': now(), 'body_sha256': job['body_sha256'], 'tables_equal': table_ok,
              'source_tables': len(expected), 'rendered_tables': len(actual), 'text_equal': expected_text == actual_text,
              'source_text_characters': len(expected_text), 'rendered_text_characters': len(actual_text),
              'structure_equal': source_structure == rendered_structure,
              'list_signatures_equal': lists_equal,
              'structure_counts': source_structure['counts'],
              'chapter_count': sum(bool(re.match(r'^第.+章', text)) for tag, text in source_structure['headings'] if tag == 'h2'),
              'uncertain_spans': len(re.findall(r'\[[^\]]+\]', expected_text)),
              'parser_warnings': parsed.get('warnings', [])}
    if expected_text != actual_text:
        report['first_text_difference'] = first_difference(expected_text, actual_text)
    json_write(work / 'conversion-validation.json', report)
    if not table_ok or expected_text != actual_text or source_structure != rendered_structure or not lists_equal:
        raise PublicationError('Wikisource parser changed text or table structure; see conversion-validation.json. No article was published.')
    return report


def prepare(args):
    md = Path(args.markdown).expanduser().resolve()
    meta, body = load_markdown(md)
    overrides = json.loads(Path(args.metadata).read_text()) if args.metadata else {}
    title = args.title or overrides.get('title') or meta.get('題名') or meta.get('title') or md.stem
    metadata = {'author': meta.get('作者', meta.get('author', '')), **overrides}
    required = ['author', 'license_wikisource', 'license_commons']
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise PublicationError('Prepare verified metadata first (agent may derive it from PDF/Commons/author sources). Missing: ' + ', '.join(missing))
    if any(ch in title for ch in '[]{}|<>\n') or ':' in title:
        raise PublicationError('Expected a plain main-namespace book title.')
    work = Path(args.workdir).expanduser().resolve() if args.workdir else md.parent / '.ocr_work/wikisource-publish' / filename(title)
    work.mkdir(parents=True, exist_ok=True)
    old = json.loads((work / 'job.json').read_text()) if (work / 'job.json').exists() else None
    commons, wiki = clients()
    remote_page = wiki.page(title)
    pdf = resolve_pdf(args.pdf, work, commons, title, metadata)
    collected = collect_images(body, md)
    scan_pages = metadata.get('image_scan_pages', {})
    if (not isinstance(scan_pages, dict)
            or set(scan_pages) - {im['target'] for im in collected}
            or any(type(page) is not int or page < 1 for page in scan_pages.values())):
        raise PublicationError('metadata.image_scan_pages must map referenced image targets to positive scan page numbers.')
    map_path = getattr(args, 'pdf_page_map', None)
    page_map = read_pdf_page_map(map_path, [im['target'] for im in collected], pdf) if map_path else {}
    images = []
    seen = set()
    previous = {im['target']: im for im in (old or {}).get('images', [])}
    for item in collected:
        if item['target'] in seen:
            continue
        seen.add(item['target'])
        path = Path(item['path'])
        if not path.is_file():
            raise PublicationError(f'Missing referenced image: {path}')
        record = {**item, **file_record(path)}
        with Image.open(path) as img:
            record.update(width=img.width, height=img.height)
        name = filename(title + ' - ' + path.stem + ' - ' + record['sha1'][:10]) + path.suffix.lower()
        page_match = re.search(r'(?:scan|page|portrait|full|p)[-_]?(\d{1,4})', path.stem, re.I)
        record.update(requested_filename=name,
                      scan_page=scan_pages.get(item['target'], int(page_match.group(1)) if page_match else None),
                      status='pending')
        prior = previous.get(item['target'])
        if item['target'] in page_map:
            record.update(source_kind='pdf_page', pdf_page=page_map[item['target']],
                          source_pdf_sha1=pdf['sha1'], commons_filename=pdf['filename'], status='reused_pdf_page')
        elif prior and prior.get('source_kind') != 'pdf_page' and prior.get('sha1') == record['sha1'] and prior.get('commons_filename'):
            record.update(commons_filename=prior['commons_filename'], status='recorded')
        images.append(record)
    categories = re.findall(r'\[\[(?:Category|分類|分类):[^\]\n]+\]\]', remote_page.get('content', ''))
    job = {'schema_version': 1, 'status': 'prepared', 'prepared_utc': now(), 'title': title,
           'markdown': file_record(md), 'metadata': metadata, 'pdf': pdf, 'images': images,
           'base_page': {k: v for k, v in remote_page.items() if k != 'content'}, 'categories': list(dict.fromkeys(categories))}
    if map_path:
        job['pdf_page_map'] = file_record(map_path)
    (work / 'previous-page.wikitext').write_text(remote_page.get('content', ''), encoding='utf-8')
    render_job(job, work)
    if not args.offline_validation:
        validate_body(job, work, wiki)
    json_write(work / 'preparation.json', {'workdir': str(work), 'title': title, 'images': len(images),
                                         'statistics': job['statistics'], 'base_revision': remote_page.get('revid'), 'pdf_status': pdf['status']})
    emit('prepared', workdir=str(work), title=title, images=len(images), statistics=job['statistics'])


def description(job, image=None):
    meta = job['metadata']
    year = str(meta.get('year', ''))
    if image is None:
        desc = f"{{{{zh|1={text_parameter(job['title'])}原文掃描。}}}}"
        source = meta.get('source')
        if not source:
            raise PublicationError('A new PDF upload needs a verifiable source description/URL in metadata.source.')
        author = text_parameter(meta['author'])
    else:
        scan = image.get('scan_page')
        desc = f"{{{{zh|1=《{text_parameter(job['title'])}》圖版" + (f'，PDF掃描第{scan}頁' if scan else '') + '。保留轉寫稿採用的圖像方向及範圍。}}'
        source = '[[:File:' + job['pdf']['filename'] + ']]' + (f'，PDF第{scan}頁。' if scan else '')
        author = meta.get('image_author', '{{unknown|author}}（圖像原作者見所據原書；本檔為掃描圖版。）')
    return ('=={{int:filedesc}}==\n{{Information\n|description=' + desc + '\n|date=' + year + '\n|source=' + source
            + '\n|author=' + author + '\n|permission=\n|other versions=\n}}\n\n=={{int:license-header}}==\n'
            + meta['license_commons'].strip() + '\n')


def save_job(work, job):
    json_write(work / 'job.json', job)


def checked_package(work, job):
    for record in [job['markdown'], job['pdf'], *job['images'], job.get('pdf_page_map', {})]:
        if record.get('path'):
            checked_local(record['path'], record['sha256'])
    checked_local(work / 'publication.wikitext', job['publication_sha256'])
    checked_local(work / 'body.wikitext', job['body_sha256'])
    _, body = load_markdown(job['markdown']['path'])
    if (work / 'source-body.html').read_text() != source_html(body):
        raise PublicationError('Source comparison artifact changed; prepare again.')
    if job.get('pdf_page_map'):
        pages = read_pdf_page_map(job['pdf_page_map']['path'], [im['target'] for im in job['images']], job['pdf'])
        recorded = {im['target']: im['pdf_page'] for im in job['images'] if im.get('source_kind') == 'pdf_page'}
        if pages != recorded:
            raise PublicationError('Prepared PDF page records differ from the reviewed map.')
    fresh = convert(body, image_mapping(job))
    if (fresh['wikitext'] != (work / 'body.wikitext').read_text()
            or fresh['expected_tables'] != json.loads((work / 'expected-tables.json').read_text())
            or fresh['images'] != job['image_order']):
        raise PublicationError('Conversion artifacts differ from the current source/converter; prepare again.')


def ensure_remote_asset(commons, record, requested, desc, job, work):
    checked_local(record['path'], record['sha256'])
    checkpoint = record.get('upload_checkpoint')
    def progress(value):
        record['upload_checkpoint'] = value
        save_job(work, job)
    result = commons.upload(record['path'], requested, desc, 'Publish scanned source illustration for ' + job['title'],
                            checkpoint=checkpoint, on_checkpoint=progress)
    info = result['file']
    if info['sha1'] != record['sha1']:
        raise PublicationError('Uploaded file hash does not match the source image/PDF.')
    if 'width' in record and (info.get('width'), info.get('height')) != (record['width'], record['height']):
        raise PublicationError('Uploaded image dimensions differ from the local source.')
    record.update(commons_filename=info['name'], remote=info, status=result['status'])
    save_job(work, job)
    return info


def publish(args):
    work = Path(args.workdir).resolve()
    job = json.loads((work / 'job.json').read_text())
    blockers = job.get('metadata', {}).get('publication_blockers', [])
    if blockers:
        raise PublicationError('Prepared package is for review only: ' + '; '.join(str(item) for item in blockers))
    checked_package(work, job)
    commons, wiki = clients()
    current = wiki.page(job['title'])
    already_published = (work / 'publication.wikitext').read_text()
    if not current.get('missing') and publication_equal(current.get('content', ''), already_published):
        emit('article_unchanged', revision=current['revid'])
        return verify(argparse.Namespace(workdir=str(work), credentials=None))
    commons, wiki = clients(args.credentials)
    if (job['pdf'].get('path') and job['pdf'].get('status') != 'reused') or any(im.get('source_kind') != 'pdf_page' for im in job['images']):
        commons.login()
    wiki.login()
    current = wiki.page(job['title'])
    base = job['base_page']
    if current.get('revid') != base.get('revid') or current.get('missing') != base.get('missing'):
        raise PublicationError('The target article changed after preparation; compare the current page and prepare again before updating.')
    if not current.get('missing') and not args.update_existing:
        raise PublicationError('Target exists. Explicit update authorization is required; use --update-existing only for an authorized update.')
    render_job(job, work)
    validate_body(job, work, wiki)
    # Materialize all assets before making the article refer to them.
    if job['pdf'].get('status') == 'reused' or not job['pdf'].get('path'):
        pdf_info = commons.file_info(job['pdf']['filename'])
        if not pdf_info or pdf_info['sha1'] != job['pdf']['sha1']:
            raise PublicationError('Commons source PDF changed since preparation.')
    else:
        pdf_info = ensure_remote_asset(commons, job['pdf'], job['pdf']['filename'], description(job), job, work)
    job['pdf'].update(filename=pdf_info['name'], remote=pdf_info)
    save_job(work, job)
    for index, image in enumerate(job['images'], 1):
        if image.get('source_kind') == 'pdf_page':
            image.update(commons_filename=pdf_info['name'], status='reused_pdf_page')
            save_job(work, job)
        else:
            requested = image.get('commons_filename', image['requested_filename'])
            ensure_remote_asset(commons, image, requested, description(job, image), job, work)
        emit('image_ready', completed=index, total=len(job['images']), filename=image['commons_filename'], status=image['status'])
    whole, _ = render_job(job, work)
    parsed = wiki.parse(whole, job['title'])
    (work / 'publication-preview.html').write_text(parsed['html'], encoding='utf-8')
    if re.search(r'class="[^"]*\berror\b', parsed['html']):
        raise PublicationError('Full-page preview contains parser/template errors. Inspect publication-preview.html before publishing.')
    if not validate_pdf_embeds(parsed['html'], job):
        raise PublicationError('PDF page previews are missing or out of order. No article was published.')
    sizeinfo = wiki.request({'action': 'query', 'meta': 'siteinfo', 'siprop': 'general'})
    limit = sizeinfo['query']['general']['maxarticlesize']
    if len(whole.encode()) > limit:
        raise PublicationError(f'Book exceeds this wiki\'s single-page limit ({limit} bytes). No split or partial article was published.')
    final_page = wiki.page(job['title'])
    if final_page.get('revid') != current.get('revid') or final_page.get('missing') != current.get('missing'):
        raise PublicationError('Article changed while assets were uploaded; assets were saved, but no article update was attempted.')
    result = wiki.edit(job['title'], whole, '依原文PDF及已校對Markdown更新全文、表格及圖版；保留待核文字',
                       base_revision=current.get('revid'), base_timestamp=current.get('timestamp'), create_only=current.get('missing', False))
    job.update(status='published', published=result, published_utc=now())
    save_job(work, job)
    emit('published', **result)
    verify(argparse.Namespace(workdir=str(work), credentials=None))


def verify(args):
    work = Path(args.workdir).resolve()
    job = json.loads((work / 'job.json').read_text())
    checked_package(work, job)
    commons, wiki = clients()
    current = wiki.page(job['title'])
    expected = (work / 'publication.wikitext').read_text()
    if current.get('missing') or not publication_equal(current.get('content', ''), expected):
        raise PublicationError('Live article is missing or differs from the prepared publication. Verification did not pass.')
    conversion = validate_body(job, work, wiki)
    pdf = commons.file_info(job['pdf']['filename'])
    pdf_ok = bool(pdf and pdf['sha1'] == job['pdf']['sha1'])
    parsed = wiki.parse(expected, job['title'])
    pdf_embeds_ok = validate_pdf_embeds(parsed['html'], job)
    image_checks = []
    for record in job['images']:
        name = record.get('commons_filename', record['requested_filename'])
        if record.get('source_kind') == 'pdf_page':
            image_checks.append({'target': record['target'], 'commons_filename': job['pdf']['filename'],
                                 'source_kind': 'pdf_page', 'pdf_page': record['pdf_page'],
                                 'source_pdf_sha1': job['pdf']['sha1'], 'local_render_sha256': record['sha256'],
                                 'ok': pdf_ok and pdf_embeds_ok, 'url': (pdf or {}).get('url')})
        else:
            info = commons.file_info(name)
            ok = info and info['sha1'] == record['sha1'] and (info['width'], info['height']) == (record['width'], record['height'])
            image_checks.append({'target': record['target'], 'commons_filename': name, 'ok': bool(ok), 'url': (info or {}).get('url')})
    (work / 'published-page.html').write_text(parsed['html'], encoding='utf-8')
    report = {'checked_utc': now(), 'page_url': current['url'], 'revision': current['revid'],
              'wikitext_matches': True, 'conversion': conversion, 'pdf_ok': pdf_ok, 'pdf_page_embeds_ok': pdf_embeds_ok,
              'pdf_url': (pdf or {}).get('description_url'), 'images': image_checks,
              'all_images_ok': all(x['ok'] for x in image_checks),
              'statistics': job['statistics'], 'browser_visual_qa': 'pending; inspect the live page in a browser'}
    json_write(work / 'verification.json', report)
    if not report['all_images_ok'] or not pdf_ok:
        raise PublicationError('Some published media failed hash/dimension verification; see verification.json.')
    job['verified_revision'] = current['revid']
    job['status'] = 'published_api_verified'
    save_job(work, job)
    emit('api_verified', page_url=current['url'], revision=current['revid'], image_count=len(image_checks), pdf_url=report['pdf_url'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare', help='Read-only remote checks; create a local publication package.')
    p.add_argument('--markdown', required=True)
    p.add_argument('--pdf', required=True)
    p.add_argument('--metadata', help='Agent-prepared JSON of verified metadata and license templates.')
    p.add_argument('--pdf-page-map', help='Reviewed JSON image_pages mapping for unmodified full pages of an existing Commons PDF.')
    p.add_argument('--title')
    p.add_argument('--workdir')
    p.add_argument('--offline-validation', action='store_true', help='Defer only server parser validation; remote source checks still run.')
    p.set_defaults(func=prepare)
    p = subs.add_parser('publish', help='Upload missing media and publish the prepared article using authorized credentials.')
    p.add_argument('--workdir', required=True)
    p.add_argument('--credentials', type=Path, default=CREDENTIALS)
    p.add_argument('--update-existing', action='store_true', help='Use only when the user authorized updating this existing title.')
    p.set_defaults(func=publish)
    p = subs.add_parser('verify', help='Re-read the live article and verify content, tables, PDF and images.')
    p.add_argument('--workdir', required=True)
    p.set_defaults(func=verify)
    args = parser.parse_args()
    try:
        args.func(args)
    except (UploadWarning, AmbiguousWriteError) as exc:
        detail = {'time_utc': now(), 'error': str(exc), 'error_type': type(exc).__name__,
                  'warnings': getattr(exc, 'warnings', {}), 'checkpoint': getattr(exc, 'checkpoint', None)}
        if getattr(args, 'workdir', None):
            json_write(Path(args.workdir) / 'write-interruption.json', detail)
        emit('stopped', error=str(exc), error_type=type(exc).__name__, details_file='write-interruption.json')
        return 1
    except (PublicationError, MediaWikiError, OSError, ValueError) as exc:
        emit('stopped', error=str(exc), error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
