"""Server parser round-trip validation, HTML structure inspection and layout advisories."""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from md_wikitext import canonical_text, list_signatures, table_signatures
from publisher.metadata import layout_mode_for
from publisher.models import PublicationError, first_difference, json_write, now


class StructureReader(HTMLParser):
    """Extract normalized heading sequence and block-level tag counts from HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = {key: 0 for key in ("h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li")}
        self.headings = []
        self.active_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.counts:
            self.counts[tag] += 1
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.active_heading = [tag, ""]

    def handle_data(self, data):
        if self.active_heading is not None:
            self.active_heading[1] += data

    def handle_endtag(self, tag):
        if self.active_heading is not None and tag == self.active_heading[0]:
            self.active_heading[1] = re.sub(r"\s+", "", self.active_heading[1])
            self.headings.append(self.active_heading)
            self.active_heading = None


def document_structure(value: str) -> dict:
    """Return heading hierarchy and list/heading tag counts for an HTML fragment."""
    reader = StructureReader()
    reader.feed(value)
    return {"counts": reader.counts, "headings": reader.headings}


def duplicate_h2_headings(structure: dict) -> list[str]:
    """Identify repeated ``h2`` headings that weaken TOC/RAG section disambiguation."""
    seen, duplicates = set(), []
    for tag, heading in structure["headings"]:
        if tag == "h2":
            if heading in seen and heading not in duplicates:
                duplicates.append(heading)
            seen.add(heading)
    return duplicates


def layout_diagnostics(job: dict, whole: str, source_structure: dict) -> tuple[str, int, list[str]]:
    """Evaluate layout mode, serialized UTF-8 byte size and SEO/GEO/accessibility advisories."""
    mode = layout_mode_for(job["metadata"])
    wikitext_bytes = len(whole.encode("utf-8"))
    advisories = []
    if mode == "single_page" and wikitext_bytes > 300_000:
        advisories.append(
            "Single-page wikitext exceeds 300 KB; for SEO/GEO and full-page fetch reliability on long or multi-subject works, "
            "consider a substantive root page + semantic subpages (~60–250 KB each)."
        )
    if mode == "subpage" and wikitext_bytes < 15_000:
        advisories.append(
            "Subpage wikitext is under 15 KB; avoid splitting short works into thin fragments unless matching a natural short volume."
        )
    if duplicate_h2_headings(source_structure):
        advisories.append(
            "Repeated H2 headings detected; normalize heading hierarchy (e.g. major parts/subjects at ##, chapters at ###) "
            "to improve navigation and RAG chunking."
        )
    return mode, wikitext_bytes, advisories


def validate_body(job: dict, work: Path, wiki) -> dict:
    """Parse stripped body wikitext on Wikisource and assert lossless text/table/list/structure parity."""
    text = (work / "body.wikitext").read_text(encoding="utf-8")
    # Image files are verified separately. Removing embeds permits losslessness checks before uploading.
    text = re.sub(r"\[\[File:.*?\]\]", "", text, flags=re.S)
    parsed = wiki.parse("__NOCONTENTCONVERT__\n__NOTOC__\n" + text, job["title"])
    actual_html = parsed["html"]
    (work / "body-preview.html").write_text(actual_html, encoding="utf-8")
    expected = json.loads((work / "expected-tables.json").read_text(encoding="utf-8"))
    actual = table_signatures(actual_html)
    table_ok = expected == actual
    if not table_ok:
        json_write(work / "actual-tables.json", actual)

    source_html_text = (work / "source-body.html").read_text(encoding="utf-8")
    expected_text = canonical_text(source_html_text)
    actual_text = canonical_text(actual_html)
    source_structure = document_structure(source_html_text)
    rendered_structure = document_structure(actual_html)
    lists_equal = list_signatures(source_html_text) == list_signatures(actual_html)
    report = {
        "checked_utc": now(),
        "body_sha256": job["body_sha256"],
        "tables_equal": table_ok,
        "source_tables": len(expected),
        "rendered_tables": len(actual),
        "text_equal": expected_text == actual_text,
        "source_text_characters": len(expected_text),
        "rendered_text_characters": len(actual_text),
        "structure_equal": source_structure == rendered_structure,
        "list_signatures_equal": lists_equal,
        "structure_counts": source_structure["counts"],
        "duplicate_h2_headings": duplicate_h2_headings(source_structure),
        "chapter_count": sum(
            bool(re.match(r"^第.+章", heading))
            for tag, heading in source_structure["headings"]
            if tag == "h2"
        ),
        "uncertain_spans": len(re.findall(r"\[[^\]]+\]", expected_text)),
        "parser_warnings": parsed.get("warnings", []),
    }
    if expected_text != actual_text:
        report["first_text_difference"] = first_difference(expected_text, actual_text)
    json_write(work / "conversion-validation.json", report)
    if not table_ok or expected_text != actual_text or source_structure != rendered_structure or not lists_equal:
        raise PublicationError(
            "Wikisource parser changed text or table structure; see conversion-validation.json. No article was published."
        )
    return report


def validate_pdf_embeds(parsed_html: str, job: dict) -> bool:
    """Verify actual rendered PDF thumbnail page numbers and ordering against expected embeds."""
    expected = [im["page"] for im in job["image_order"] if "page" in im]
    if not expected:
        return True
    pdf_name = job["pdf"]["filename"].replace("_", " ")

    class _EmbedReader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.pages = []

        def handle_starttag(self, tag, attrs):
            if tag != "img":
                return
            src = dict(attrs).get("src", "")
            parts = unquote(urlparse(src).path).split("/")
            for index, part in enumerate(parts[:-1]):
                if part.replace("_", " ") == pdf_name:
                    match = re.match(r"page(\d+)-", parts[index + 1])
                    if match:
                        self.pages.append(int(match.group(1)))

    reader = _EmbedReader()
    reader.feed(parsed_html)
    meta = job.get("metadata", {})
    has_header_pdf_thumb = not (meta.get("cover_image") or meta.get("omit_pdf_thumb"))
    target_pages = [1, *expected] if has_header_pdf_thumb else expected
    return reader.pages == target_pages
