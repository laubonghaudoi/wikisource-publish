"""Metadata validation, Wikisource header/TOC rendering, edit summaries and Commons descriptions."""
from __future__ import annotations

import re

from publisher.models import PublicationError, text_parameter

CATEGORY_PREFIXES = ("Category:", "分類:", "分类:")
FILE_PREFIXES = ("File:", "Image:", "檔案:", "文件:")
_UNESCAPED_FILE_LINK_RE = re.compile(r"\[\[(?!:)(?:File|Image|檔案|文件):", re.IGNORECASE)
_REMOTE_CATEGORY_RE = re.compile(
    r"\[\[(?:" + "|".join(re.escape(prefix[:-1]) for prefix in CATEGORY_PREFIXES) + r"):[^\]\n]+\]\]"
)


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    lowered = value.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix.casefold()):
            return value[len(prefix) :].strip()
    return value


def validate_wiki_page_target(value, field_name: str, *, allow_relative: bool = False) -> str:
    """Validate and return a main-namespace or relative Wikisource link target."""
    if not isinstance(value, str):
        raise PublicationError(f"{field_name} must be a string page title.")
    target = value.strip()
    if not target or target != value:
        raise PublicationError(
            f"{field_name} must be a non-empty page title without leading or trailing whitespace."
        )
    if any(ch in target for ch in "[]{}|<>\n\r:#") or "//" in target or target.endswith("/"):
        raise PublicationError(f"{field_name} contains unsupported characters or path syntax: {target!r}")
    if target in {".", "..", "../"} or target.startswith("./"):
        raise PublicationError(f"{field_name} must name a concrete target page.")
    if not allow_relative and (target.startswith("/") or target.startswith("../")):
        raise PublicationError(
            f"{field_name} must be a root main-namespace title, not a relative link: {target!r}"
        )
    rest = target[3:] if target.startswith("../") else target.lstrip("/")
    if not rest or any(part in {"", ".", ".."} for part in rest.split("/")):
        raise PublicationError(f"{field_name} contains invalid path segments: {target!r}")
    return target


def format_header_link(value, field_name: str) -> str:
    """Format a previous/next header link string or ``{"page", "label"}`` mapping."""
    if isinstance(value, str):
        page = validate_wiki_page_target(value, field_name, allow_relative=True)
        return f"[[{page}]]"
    if isinstance(value, dict):
        unknown = set(value) - {"page", "label"}
        if unknown or "page" not in value:
            raise PublicationError(f'{field_name} object must contain "page" and optional "label".')
        page = validate_wiki_page_target(value["page"], f"{field_name}.page", allow_relative=True)
        label = value.get("label")
        if label in (None, ""):
            return f"[[{page}]]"
        if not isinstance(label, str) or not label.strip():
            raise PublicationError(f"{field_name}.label must be a non-empty string.")
        return f"[[{page}|{text_parameter(label.strip())}]]"
    raise PublicationError(f'{field_name} must be a page title string or {{"page", "label"}} object.')


def validate_subpages(subpages, root_title: str) -> list[dict]:
    """Validate ``metadata.subpages`` entries for a substantive root page."""
    if not isinstance(subpages, list) or not subpages:
        raise PublicationError("metadata.subpages must be a non-empty list.")
    seen = set()
    normalized = []
    for index, item in enumerate(subpages):
        if not isinstance(item, dict):
            raise PublicationError(f"metadata.subpages[{index}] must be an object.")
        unknown = set(item) - {"page", "label", "summary"}
        if unknown or "page" not in item or "label" not in item:
            raise PublicationError(
                f'metadata.subpages[{index}] requires "page" and "label" (optional "summary").'
            )
        page = validate_wiki_page_target(
            item["page"], f"metadata.subpages[{index}].page", allow_relative=True
        )
        if not (page.startswith("/") or page.startswith(root_title + "/")):
            raise PublicationError(
                f'metadata.subpages[{index}].page must be a subpage of {root_title!r} '
                f'(start with "/" or "{root_title}/").'
            )
        if page in seen:
            raise PublicationError(f"Duplicate subpage target in metadata.subpages: {page!r}")
        seen.add(page)
        label = item["label"]
        if not isinstance(label, str) or not label.strip():
            raise PublicationError(f"metadata.subpages[{index}].label must be a non-empty string.")
        summary = "" if item.get("summary") is None else item.get("summary")
        if not isinstance(summary, str):
            raise PublicationError(f"metadata.subpages[{index}].summary must be a string.")
        normalized.append({"page": page, "label": label.strip(), "summary": summary.strip()})
    return normalized


def normalize_categories(raw_categories) -> list[str]:
    """Normalize plain or prefixed category names to canonical ``[[Category:...]]`` tags."""
    if raw_categories is None:
        return []
    if not isinstance(raw_categories, list):
        raise PublicationError("metadata.categories must be a list of category names.")
    result = []
    for index, item in enumerate(raw_categories):
        if not isinstance(item, str) or not item.strip():
            raise PublicationError(f"metadata.categories[{index}] must be a non-empty string.")
        name = _strip_prefix(item.strip(), CATEGORY_PREFIXES)
        if not name or any(ch in name for ch in "[]{}|<>\n\r:#"):
            raise PublicationError(f"Invalid category name in metadata.categories[{index}]: {item!r}")
        result.append(f"[[Category:{name}]]")
    return result


def extract_remote_categories(wikitext: str) -> list[str]:
    """Extract existing category tags from remote page wikitext."""
    return _REMOTE_CATEGORY_RE.findall(wikitext or "")


def _validate_single_line_str(value, field_name: str) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value or not value.strip():
        raise PublicationError(f"{field_name} must be a non-empty single-line string.")
    return value.strip()


def validate_metadata_structure(title: str, metadata: dict) -> None:
    """Validate structural relationships, links and formatting safety of page metadata."""
    section = metadata.get("section")
    if section is not None:
        if not isinstance(section, str) or not section.strip():
            raise PublicationError("metadata.section must be a non-empty string when provided.")
        section = section.strip()
    subpages = metadata.get("subpages")
    if section and subpages:
        raise PublicationError("A subpage (metadata.section) cannot also define metadata.subpages.")
    if section:
        parent = metadata.get("parent_title")
        if parent is not None:
            validate_wiki_page_target(parent, "metadata.parent_title", allow_relative=False)
        elif "/" not in title:
            raise PublicationError(
                'Subpage with metadata.section must use a "Book/Subpage" title or provide metadata.parent_title.'
            )
        if metadata.get("categories"):
            raise PublicationError("Place categories on the root page rather than individual subpages (metadata.section).")
    else:
        if metadata.get("parent_title") is not None:
            raise PublicationError("metadata.parent_title requires metadata.section.")
        if subpages is not None:
            validate_subpages(subpages, title)
    if metadata.get("categories") is not None:
        normalize_categories(metadata.get("categories"))
    for key in ("previous", "next"):
        if metadata.get(key) not in (None, ""):
            format_header_link(metadata[key], f"metadata.{key}")
    if metadata.get("edit_summary") is not None:
        _validate_single_line_str(metadata["edit_summary"], "metadata.edit_summary")
    if metadata.get("override_collector") is not None:
        _validate_single_line_str(metadata["override_collector"], "metadata.override_collector")
        if metadata.get("lawmaker"):
            raise PublicationError("Cannot combine metadata.override_collector with metadata.lawmaker.")
    notes = metadata.get("notes")
    if notes not in (None, "") and _UNESCAPED_FILE_LINK_RE.search(str(notes)):
        raise PublicationError(
            "Use [[:File:...]] (with a leading colon) in metadata.notes so thumbnails do not embed inside the header notes bar."
        )
    if metadata.get("cover_image") not in (None, ""):
        cover = _strip_prefix(_validate_single_line_str(metadata["cover_image"], "metadata.cover_image"), FILE_PREFIXES)
        if not cover or any(ch in cover for ch in "[]{}|<>#"):
            raise PublicationError(f"Invalid metadata.cover_image filename: {metadata['cover_image']!r}")
    if metadata.get("omit_pdf_thumb") not in (None, True, False):
        raise PublicationError("metadata.omit_pdf_thumb must be a boolean.")


def missing_required_fields(metadata: dict) -> list[str]:
    """Return missing required bibliographic or licensing metadata keys."""
    missing = []
    if not (metadata.get("author") or metadata.get("lawmaker") or metadata.get("override_collector")):
        missing.append("author")
    if not metadata.get("license_commons"):
        missing.append("license_commons")
    if not metadata.get("section") and not metadata.get("license_wikisource"):
        missing.append("license_wikisource")
    return missing


def layout_mode_for(metadata: dict) -> str:
    """Return ``'subpage'``, ``'root_with_subpages'``, or ``'single_page'``."""
    if metadata.get("section"):
        return "subpage"
    if metadata.get("subpages"):
        return "root_with_subpages"
    return "single_page"


def make_header(job: dict) -> str:
    """Build the ``{{header}}`` or ``{{Collection header}}`` block plus optional top scan/cover."""
    meta = job["metadata"]
    validate_metadata_structure(job["title"], meta)
    if meta.get("section"):
        parent = meta.get("parent_title") or job["title"].split("/", 1)[0]
        header_title = f"[[{validate_wiki_page_target(parent, 'metadata.parent_title', allow_relative=False)}]]"
    else:
        header_title = text_parameter(job["title"])

    fields = {"title": header_title}
    use_collection = bool(meta.get("override_collector")) or meta.get("header_template") == "collection"
    if use_collection:
        collector = meta.get("override_collector") or meta.get("author") or meta.get("lawmaker")
        if collector:
            fields["override_collector"] = text_parameter(collector)
    else:
        fields["lawmaker" if meta.get("lawmaker") else "author"] = text_parameter(
            meta.get("lawmaker") or meta["author"]
        )

    if meta.get("section"):
        fields["section"] = text_parameter(meta["section"].strip())
    for key in ("year", "month", "day", "edition", "notes"):
        if meta.get(key) not in (None, ""):
            fields[key] = text_parameter(meta[key])
    for key in ("previous", "next"):
        if meta.get(key) not in (None, ""):
            fields[key] = format_header_link(meta[key], f"metadata.{key}")

    template_name = "Collection header" if use_collection else "header"
    header = "{{" + template_name + "\n" + "".join(f" | {k} = {v}\n" for k, v in fields.items()) + "}}"

    if meta.get("cover_image"):
        cover = _strip_prefix(meta["cover_image"].strip(), FILE_PREFIXES)
        return f"{header}\n\n[[File:{cover}|360px|center|{text_parameter(job['title'])}封面]]\n\n"
    if meta.get("omit_pdf_thumb"):
        return f"{header}\n\n"
    return f"{header}\n\n[[File:{job['pdf']['filename']}|thumb|{text_parameter(job['title'])}原文掃描]]\n\n"


def make_subpage_toc(job: dict) -> str:
    """Build the ``== 目錄 ==`` section for a substantive root page when subpages exist."""
    subpages = job["metadata"].get("subpages")
    if not subpages:
        return ""
    items = validate_subpages(subpages, job["title"])
    lines = ["== 目錄 =="]
    for item in items:
        link = f"[[{item['page']}|{text_parameter(item['label'])}]]"
        lines.append(f"* {link}：{text_parameter(item['summary'])}" if item["summary"] else f"* {link}")
    return "\n".join(lines)


def default_edit_summary(job: dict, is_new: bool) -> str:
    """Generate an explicit edit summary matching custom overrides or layout mode."""
    custom = job["metadata"].get("edit_summary")
    if custom:
        return custom.strip()
    mode = layout_mode_for(job["metadata"])
    verb = "建立" if is_new else "更新"
    if mode == "root_with_subpages":
        return f"依原文PDF及已校對Markdown{verb}作品根頁、書目導讀與子頁目錄"
    if mode == "subpage":
        return f"依原文PDF及已校對Markdown{verb}分頁正文、表格及圖版；保留待核文字"
    return f"依原文PDF及已校對Markdown{verb}全文、表格及圖版；保留待核文字"


def description(job: dict, image: dict | None = None) -> str:
    """Build the Commons ``{{Information}}`` and license block for PDF or extracted plates."""
    meta = job["metadata"]
    year = str(meta.get("year", ""))
    creator = meta.get("author") or meta.get("override_collector") or meta.get("lawmaker") or ""
    if image is None:
        desc = f"{{{{zh|1={text_parameter(job['title'])}原文掃描。}}}}"
        source = meta.get("source")
        if not source:
            raise PublicationError("A new PDF upload needs a verifiable source description/URL in metadata.source.")
        author = text_parameter(creator)
    else:
        scan = image.get("scan_page")
        desc = (
            f"{{{{zh|1=《{text_parameter(job['title'])}》圖版"
            + (f"，PDF掃描第{scan}頁" if scan else "")
            + "。保留轉寫稿採用的圖像方向及範圍。}}"
        )
        source = "[[:File:" + job["pdf"]["filename"] + "]]" + (f"，PDF第{scan}頁。" if scan else "")
        author = meta.get("image_author", "{{unknown|author}}（圖像原作者見所據原書；本檔為掃描圖版。）")
    return (
        "=={{int:filedesc}}==\n{{Information\n|description="
        + desc
        + "\n|date="
        + year
        + "\n|source="
        + source
        + "\n|author="
        + author
        + "\n|permission=\n|other versions=\n}}\n\n=={{int:license-header}}==\n"
        + meta["license_commons"].strip()
        + "\n"
    )
