"""Independent PDF-page reuse regressions; clients and writes are mocked."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_publish_workflow as fixture

workflow = fixture.workflow


class PdfPageWorkflowTests(unittest.TestCase):
    # Reuse fixture methods without inheriting and rerunning its test methods.
    setUp = fixture.PublishWorkflowTests.setUp
    make_job = fixture.PublishWorkflowTests.make_job

    def pdf_job(self, page=15, repeats=1):
        job = self.make_job(image=True)
        job['images'][0].update(source_kind='pdf_page', pdf_page=page,
                                source_pdf_sha1=job['pdf']['sha1'],
                                commons_filename=job['pdf']['filename'], status='reused_pdf_page')
        if repeats > 1:
            with self.md.open('a', encoding='utf-8') as stream:
                stream.write('\n\n![再次引用](scan-001.png)\n' * (repeats - 1))
            job['markdown'] = workflow.file_record(self.md)
        workflow.render_job(job, self.work)
        return job

    def map_file(self, data):
        path = self.root / 'pdf-page-map.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def thumbnails(self, job, pages, *, filename=None):
        name = quote((filename or job['pdf']['filename']).replace(' ', '_'), safe='')
        images = []
        for page in pages:
            base = f'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/page{page}-'
            images.append(f'<span typeof="mw:File"><a href="/wiki/File:{name}?page={page}">'
                          f'<img src="{base}800px-{name}.jpg" '
                          f'srcset="{base}1200px-{name}.jpg 1.5x, {base}1600px-{name}.jpg 2x" '
                          f'width="800" height="1100"></a></span>')
        return '<div class="mw-parser-output">' + ''.join(images) + '</div>'

    def clients(self, job, *, content=None, pages=(1, 15)):
        commons, wiki = fixture.PublishWorkflowTests.fake_clients(self, job, content)
        # A PDF page is checked against its source PDF hash, not the PNG hash.
        commons.file_info.side_effect = lambda name: dict(self.pdf_info) if name == job['pdf']['filename'] else None
        body_html = (self.work / 'source-body.html').read_text()
        wiki.parse.side_effect = lambda text, title: {
            'html': self.thumbnails(job, pages) if '{{header' in text else body_html,
            'warnings': [], 'images': [job['pdf']['filename']],
        }
        return commons, wiki

    def test_page_map_accepts_known_targets_and_positive_pages(self):
        job = self.make_job()
        path = self.map_file({'image_pages': {'scan-001.png': 15}})
        self.assertEqual(workflow.read_pdf_page_map(path, {'scan-001.png'}, job['pdf']), {'scan-001.png': 15})

    def test_page_map_rejects_invalid_numbers_unknown_targets_and_empty_maps(self):
        job = self.make_job()
        invalid = [{'image_pages': {'scan-001.png': page}}
                   for page in (0, -1, True, False, None, '15', 1.5)]
        invalid += [{'image_pages': {'unknown.png': 1}}, {'image_pages': {}}, {'image_pages': []}, {}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(workflow.PublicationError):
                workflow.read_pdf_page_map(self.map_file(data), {'scan-001.png'}, job['pdf'])

    def test_page_map_requires_an_existing_pdf(self):
        job = self.make_job()
        path = self.map_file({'image_pages': {'scan-001.png': 15}})
        for changed in ({'status': 'pending'}, {'status': 'uploaded'}, {'filename': 'Source.png'}):
            with self.subTest(changed=changed), self.assertRaises(workflow.PublicationError):
                workflow.read_pdf_page_map(path, {'scan-001.png'}, {**job['pdf'], **changed})

    def test_page_map_checks_recorded_page_limit_and_source_hash(self):
        job = self.make_job()
        data = {'image_pages': {'scan-001.png': 15},
                'evidence': {'source_pdf_pages': 20, 'source_pdf': str(self.pdf),
                             'source_pdf_sha256': workflow.digest(self.pdf)}}
        self.assertEqual(workflow.read_pdf_page_map(self.map_file(data), {'scan-001.png'}, job['pdf']), {'scan-001.png': 15})
        data['evidence']['source_pdf_pages'] = 14
        with self.assertRaises(workflow.PublicationError):
            workflow.read_pdf_page_map(self.map_file(data), {'scan-001.png'}, job['pdf'])
        data['evidence']['source_pdf_pages'] = 20
        self.pdf.write_bytes(self.pdf.read_bytes() + b'changed source')
        with self.assertRaises(workflow.PublicationError):
            workflow.read_pdf_page_map(self.map_file(data), {'scan-001.png'}, job['pdf'])

    def test_image_mapping_uses_pdf_identity_and_keeps_plain_mapping(self):
        job = self.pdf_job()
        job['images'].append({'target': 'plain.png', 'requested_filename': 'Plain.png'})
        self.assertEqual(workflow.image_mapping(job), {
            'scan-001.png': {'filename': job['pdf']['filename'], 'page': 15},
            'plain.png': 'Plain.png',
        })

    def test_image_mapping_rejects_wrong_pdf_hash_or_page(self):
        job = self.pdf_job()
        for changed in ({'source_pdf_sha1': '0' * 40}, {'pdf_page': 0}, {'pdf_page': True}, {'pdf_page': '15'}):
            with self.subTest(changed=changed):
                altered = {**job, 'images': [{**job['images'][0], **changed}]}
                with self.assertRaises(workflow.PublicationError):
                    workflow.image_mapping(altered)

    def test_render_records_each_repeated_pdf_page_occurrence(self):
        job = self.pdf_job(repeats=2)
        self.assertEqual([image['page'] for image in job['image_order']], [15, 15])
        self.assertEqual((self.work / 'body.wikitext').read_text().count('|page=15|frameless|'), 2)
        self.assertTrue(workflow.validate_pdf_embeds(self.thumbnails(job, [1, 15, 15]), job))
        self.assertFalse(workflow.validate_pdf_embeds(self.thumbnails(job, [1, 15]), job))

    def test_embed_validation_requires_cover_then_exact_body_page_order(self):
        job = self.pdf_job()
        self.assertTrue(workflow.validate_pdf_embeds(self.thumbnails(job, [1, 15]), job))
        for pages in ([], [15], [1], [1, 14], [15, 1], [1, 15, 15]):
            with self.subTest(pages=pages):
                self.assertFalse(workflow.validate_pdf_embeds(self.thumbnails(job, pages), job))
        self.assertFalse(workflow.validate_pdf_embeds(self.thumbnails(job, [1, 15], filename='Other.pdf'), job))

    def test_verify_rejects_wrong_rendered_pdf_page(self):
        job = self.pdf_job()
        content = (self.work / 'publication.wikitext').read_text()
        commons, wiki = self.clients(job, content=content, pages=(1, 14))
        with patch.object(workflow, 'clients', return_value=(commons, wiki)), patch.object(workflow, 'emit'):
            with self.assertRaises(workflow.PublicationError):
                workflow.verify(argparse.Namespace(workdir=str(self.work)))
        report = json.loads((self.work / 'verification.json').read_text())
        self.assertTrue(report['pdf_ok'])
        self.assertFalse(report['pdf_page_embeds_ok'])
        self.assertFalse(report['all_images_ok'])
        commons.upload.assert_not_called()
        wiki.edit.assert_not_called()

    def test_verify_rejects_saved_page_map_tamper_before_api_access(self):
        for changed in ({'pdf_page': 16}, {'source_pdf_sha1': '0' * 40}):
            with self.subTest(changed=changed):
                job = self.pdf_job()
                job['images'][0].update(changed)
                workflow.save_job(self.work, job)
                with patch.object(workflow, 'clients') as factory:
                    with self.assertRaises(workflow.PublicationError):
                        workflow.verify(argparse.Namespace(workdir=str(self.work)))
                factory.assert_not_called()

    def test_publish_pdf_page_reuse_never_authenticates_or_uploads_to_commons(self):
        job = self.pdf_job()
        commons, wiki = self.clients(job)
        with patch.object(workflow, 'clients', return_value=(commons, wiki)), \
             patch.object(workflow, 'verify'), patch.object(workflow, 'emit'):
            workflow.publish(argparse.Namespace(workdir=str(self.work), credentials=None, update_existing=False))
        commons.login.assert_not_called()
        commons.upload.assert_not_called()
        wiki.login.assert_called_once()
        wiki.edit.assert_called_once()
        self.assertIn('|page=15|frameless|', wiki.edit.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
