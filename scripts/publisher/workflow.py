"""Prepare, publish and verify workflow orchestration for Wikisource books."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from md_wikitext import collect_images, convert, load_markdown, source_html
from publisher.media import (
    collect_and_prepare_images,
    ensure_remote_asset,
    image_mapping,
    read_pdf_page_map,
    resolve_pdf,
)
from publisher.metadata import (
    default_edit_summary,
    description,
    extract_remote_categories,
    make_header,
    make_subpage_toc,
    missing_required_fields,
    normalize_categories,
    validate_metadata_structure,
)
from publisher.models import (
    PublicationError,
    checked_local,
    clients,
    emit,
    file_record,
    filename,
    json_write,
    now,
    publication_equal,
    save_job,
)
from publisher.validation import document_structure, layout_diagnostics, validate_body, validate_pdf_embeds


@dataclass(frozen=True)
class RuntimeHooks:
    """Injectable IO and side-effect dependencies for deterministic unit testing."""

    clients_fn: Callable[..., tuple[Any, Any]] = clients
    emit_fn: Callable[..., None] = emit
    verify_fn: Callable[..., Any] | None = None


def assemble_publication(job: dict, body_wiki: str) -> str:
    """Combine magic word, header, validated body, TOC, license and categories."""
    subpages = job["metadata"].get("subpages")
    if subpages and not body_wiki.strip():
        raise PublicationError(
            "Root page with metadata.subpages must include substantive introductory/bibliographic prose, not an empty directory."
        )
    categories = "\n".join(job.get("categories", []))
    # The source has historic glyph variants; do not let Chinese variant conversion rewrite them.
    parts = ["__NOCONTENTCONVERT__\n" + make_header(job) + body_wiki.rstrip()]
    toc = make_subpage_toc(job)
    if toc:
        parts.append(toc)
    license_text = (job["metadata"].get("license_wikisource") or "").strip()
    if license_text:
        parts.append(license_text)
    if categories:
        parts.append(categories)
    return "\n\n".join(parts).rstrip("\n")


def render_job(job: dict, work: Path) -> tuple[str, str]:
    """Convert prepared Markdown into body/full wikitext and persist package artifacts."""
    checked_local(job["markdown"]["path"], job["markdown"]["sha256"])
    _, body = load_markdown(job["markdown"]["path"])
    converted = convert(body, image_mapping(job))
    body_wiki = converted["wikitext"]
    whole = assemble_publication(job, body_wiki)
    (work / "body.wikitext").write_text(body_wiki, encoding="utf-8")
    (work / "publication.wikitext").write_text(whole, encoding="utf-8")
    (work / "source-body.html").write_text(source_html(body), encoding="utf-8")
    json_write(work / "expected-tables.json", converted["expected_tables"])
    job["statistics"] = converted["statistics"]
    job["publication_sha256"] = hashlib.sha256(whole.encode("utf-8")).hexdigest()
    job["body_sha256"] = hashlib.sha256(body_wiki.encode("utf-8")).hexdigest()
    job["image_order"] = converted["images"]
    save_job(work, job)
    return whole, body_wiki


def checked_package(work: Path, job: dict) -> None:
    """Re-verify every local source file and derived build artifact before API calls."""
    for record in [job["markdown"], job["pdf"], *job["images"], job.get("pdf_page_map", {})]:
        if record.get("path"):
            checked_local(record["path"], record["sha256"])
    checked_local(work / "publication.wikitext", job["publication_sha256"])
    checked_local(work / "body.wikitext", job["body_sha256"])
    _, body = load_markdown(job["markdown"]["path"])
    if (work / "source-body.html").read_text(encoding="utf-8") != source_html(body):
        raise PublicationError("Source comparison artifact changed; prepare again.")
    if job.get("pdf_page_map"):
        pages = read_pdf_page_map(
            job["pdf_page_map"]["path"], [im["target"] for im in job["images"]], job["pdf"]
        )
        recorded = {im["target"]: im["pdf_page"] for im in job["images"] if im.get("source_kind") == "pdf_page"}
        if pages != recorded:
            raise PublicationError("Prepared PDF page records differ from the reviewed map.")
    fresh = convert(body, image_mapping(job))
    if (
        fresh["wikitext"] != (work / "body.wikitext").read_text(encoding="utf-8")
        or fresh["expected_tables"] != json.loads((work / "expected-tables.json").read_text(encoding="utf-8"))
        or fresh["images"] != job["image_order"]
        or assemble_publication(job, fresh["wikitext"]) != (work / "publication.wikitext").read_text(encoding="utf-8")
    ):
        raise PublicationError("Conversion artifacts differ from the current source/converter; prepare again.")


def _assert_remote_unchanged(current: dict, expected: dict, message: str) -> None:
    if current.get("revid") != expected.get("revid") or current.get("missing") != expected.get("missing"):
        raise PublicationError(message)


def _materialize_media(job: dict, work: Path, commons, emit_fn: Callable[..., None]) -> dict:
    if job["pdf"].get("status") == "reused" or not job["pdf"].get("path"):
        pdf_info = commons.file_info(job["pdf"]["filename"])
        if not pdf_info or pdf_info["sha1"] != job["pdf"]["sha1"]:
            raise PublicationError("Commons source PDF changed since preparation.")
    else:
        pdf_info = ensure_remote_asset(
            commons, job["pdf"], job["pdf"]["filename"], description(job), job, work
        )
    job["pdf"].update(filename=pdf_info["name"], remote=pdf_info)
    save_job(work, job)

    for index, image in enumerate(job["images"], 1):
        if image.get("source_kind") == "pdf_page":
            image.update(commons_filename=pdf_info["name"], status="reused_pdf_page")
            save_job(work, job)
        else:
            requested = image.get("commons_filename", image["requested_filename"])
            ensure_remote_asset(commons, image, requested, description(job, image), job, work)
        emit_fn(
            "image_ready",
            completed=index,
            total=len(job["images"]),
            filename=image["commons_filename"],
            status=image["status"],
        )
    return pdf_info


def _verify_media_records(job: dict, commons, pdf_info: dict | None, pdf_embeds_ok: bool) -> list[dict]:
    pdf_ok = bool(pdf_info and pdf_info["sha1"] == job["pdf"]["sha1"])
    checks = []
    for record in job["images"]:
        name = record.get("commons_filename", record["requested_filename"])
        if record.get("source_kind") == "pdf_page":
            checks.append({
                "target": record["target"],
                "commons_filename": job["pdf"]["filename"],
                "source_kind": "pdf_page",
                "pdf_page": record["pdf_page"],
                "source_pdf_sha1": job["pdf"]["sha1"],
                "local_render_sha256": record["sha256"],
                "ok": pdf_ok and pdf_embeds_ok,
                "url": (pdf_info or {}).get("url"),
            })
        else:
            info = commons.file_info(name)
            ok = bool(
                info
                and info["sha1"] == record["sha1"]
                and (info["width"], info["height"]) == (record["width"], record["height"])
            )
            checks.append({
                "target": record["target"],
                "commons_filename": name,
                "ok": ok,
                "url": (info or {}).get("url"),
            })
    return checks


def prepare(args, *, hooks: RuntimeHooks | None = None) -> dict:
    """Validate sources, resolve PDF/image plans, render wikitext and save local package."""
    hooks = hooks or RuntimeHooks()
    md = Path(args.markdown).expanduser().resolve()
    meta, body = load_markdown(md)
    overrides = json.loads(Path(args.metadata).read_text(encoding="utf-8")) if args.metadata else {}
    title = args.title or overrides.get("title") or meta.get("題名") or meta.get("title") or md.stem
    metadata = {"author": meta.get("作者", meta.get("author", "")), **overrides}
    if any(ch in title for ch in "[]{}|<>\n") or ":" in title:
        raise PublicationError("Expected a plain main-namespace book title.")
    validate_metadata_structure(title, metadata)
    missing = missing_required_fields(metadata)
    if missing:
        raise PublicationError(
            "Prepare verified metadata first (agent may derive it from PDF/Commons/author sources). Missing: "
            + ", ".join(missing)
        )

    work = (
        Path(args.workdir).expanduser().resolve()
        if args.workdir
        else md.parent / ".ocr_work/wikisource-publish" / filename(title)
    )
    work.mkdir(parents=True, exist_ok=True)
    old = json.loads((work / "job.json").read_text(encoding="utf-8")) if (work / "job.json").exists() else None
    commons, wiki = hooks.clients_fn()
    remote_page = wiki.page(title)
    pdf = resolve_pdf(args.pdf, work, commons, title, metadata)

    targets = [item["target"] for item in collect_images(body, md)]
    map_path = getattr(args, "pdf_page_map", None)
    page_map = read_pdf_page_map(map_path, targets, pdf) if map_path else {}
    images = collect_and_prepare_images(
        body, md, title, metadata, pdf, (old or {}).get("images", []), page_map
    )

    remote_categories = extract_remote_categories(remote_page.get("content", ""))
    configured_categories = [] if metadata.get("section") else normalize_categories(metadata.get("categories"))
    categories = list(dict.fromkeys([*remote_categories, *configured_categories]))

    job = {
        "schema_version": 1,
        "status": "prepared",
        "prepared_utc": now(),
        "title": title,
        "markdown": file_record(md),
        "metadata": metadata,
        "pdf": pdf,
        "images": images,
        "base_page": {k: v for k, v in remote_page.items() if k != "content"},
        "categories": categories,
    }
    if map_path:
        job["pdf_page_map"] = file_record(map_path)
    (work / "previous-page.wikitext").write_text(remote_page.get("content", ""), encoding="utf-8")

    whole, _ = render_job(job, work)
    if not args.offline_validation:
        validate_body(job, work, wiki)
    source_structure = document_structure((work / "source-body.html").read_text(encoding="utf-8"))
    mode, wikitext_bytes, advisories = layout_diagnostics(job, whole, source_structure)
    prep = {
        "workdir": str(work),
        "title": title,
        "layout_mode": mode,
        "wikitext_bytes": wikitext_bytes,
        "images": len(images),
        "statistics": job["statistics"],
        "base_revision": remote_page.get("revid"),
        "pdf_status": pdf["status"],
        "layout_advisories": advisories,
    }
    json_write(work / "preparation.json", prep)
    hooks.emit_fn(
        "prepared",
        workdir=str(work),
        title=title,
        layout_mode=mode,
        wikitext_bytes=wikitext_bytes,
        images=len(images),
        statistics=job["statistics"],
        layout_advisories=advisories,
    )
    return prep


def publish(args, *, hooks: RuntimeHooks | None = None):
    """Upload missing media, re-validate full wikitext, publish article and trigger verify."""
    hooks = hooks or RuntimeHooks()
    work = Path(args.workdir).resolve()
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    blockers = job.get("metadata", {}).get("publication_blockers", [])
    if blockers:
        raise PublicationError("Prepared package is for review only: " + "; ".join(str(item) for item in blockers))
    checked_package(work, job)

    _, anon_wiki = hooks.clients_fn()
    current = anon_wiki.page(job["title"])
    already_published = (work / "publication.wikitext").read_text(encoding="utf-8")
    if not current.get("missing") and publication_equal(current.get("content", ""), already_published):
        hooks.emit_fn("article_unchanged", revision=current["revid"])
        verify_fn = hooks.verify_fn or (lambda ns: verify(ns, hooks=hooks))
        return verify_fn(argparse.Namespace(workdir=str(work), credentials=None))

    commons, wiki = hooks.clients_fn(args.credentials)
    needs_commons = (job["pdf"].get("path") and job["pdf"].get("status") != "reused") or any(
        im.get("source_kind") != "pdf_page" for im in job["images"]
    )
    if needs_commons:
        commons.login()
    wiki.login()

    current = wiki.page(job["title"])
    _assert_remote_unchanged(
        current,
        job["base_page"],
        "The target article changed after preparation; compare the current page and prepare again before updating.",
    )
    if not current.get("missing") and not args.update_existing:
        raise PublicationError(
            "Target exists. Explicit update authorization is required; use --update-existing only for an authorized update."
        )

    validate_body(job, work, wiki)
    _materialize_media(job, work, commons, hooks.emit_fn)
    whole, _ = render_job(job, work)

    parsed = wiki.parse(whole, job["title"])
    (work / "publication-preview.html").write_text(parsed["html"], encoding="utf-8")
    if re.search(r'class="[^"]*\berror\b', parsed["html"]):
        raise PublicationError(
            "Full-page preview contains parser/template errors. Inspect publication-preview.html before publishing."
        )
    if not validate_pdf_embeds(parsed["html"], job):
        raise PublicationError("PDF page previews are missing or out of order. No article was published.")

    sizeinfo = wiki.request({"action": "query", "meta": "siteinfo", "siprop": "general"})
    limit = sizeinfo["query"]["general"]["maxarticlesize"]
    if len(whole.encode("utf-8")) > limit:
        raise PublicationError(
            f"Book exceeds this wiki's single-page limit ({limit} bytes). No split or partial article was published."
        )

    final_page = wiki.page(job["title"])
    _assert_remote_unchanged(
        final_page,
        current,
        "Article changed while assets were uploaded; assets were saved, but no article update was attempted.",
    )
    result = wiki.edit(
        job["title"],
        whole,
        default_edit_summary(job, current.get("missing", False)),
        base_revision=current.get("revid"),
        base_timestamp=current.get("timestamp"),
        create_only=current.get("missing", False),
    )
    job.update(status="published", published=result, published_utc=now())
    save_job(work, job)
    hooks.emit_fn("published", **result)
    verify_fn = hooks.verify_fn or (lambda ns: verify(ns, hooks=hooks))
    return verify_fn(argparse.Namespace(workdir=str(work), credentials=None))


def verify(args, *, hooks: RuntimeHooks | None = None) -> dict:
    """Re-read the live article and verify wikitext, tables, PDF and all images via API."""
    hooks = hooks or RuntimeHooks()
    work = Path(args.workdir).resolve()
    job = json.loads((work / "job.json").read_text(encoding="utf-8"))
    checked_package(work, job)

    commons, wiki = hooks.clients_fn()
    current = wiki.page(job["title"])
    expected = (work / "publication.wikitext").read_text(encoding="utf-8")
    if current.get("missing") or not publication_equal(current.get("content", ""), expected):
        raise PublicationError(
            "Live article is missing or differs from the prepared publication. Verification did not pass."
        )

    conversion = validate_body(job, work, wiki)
    pdf_info = commons.file_info(job["pdf"]["filename"])
    pdf_ok = bool(pdf_info and pdf_info["sha1"] == job["pdf"]["sha1"])
    parsed = wiki.parse(expected, job["title"])
    pdf_embeds_ok = validate_pdf_embeds(parsed["html"], job)
    image_checks = _verify_media_records(job, commons, pdf_info, pdf_embeds_ok)

    (work / "published-page.html").write_text(parsed["html"], encoding="utf-8")
    report = {
        "checked_utc": now(),
        "page_url": current["url"],
        "revision": current["revid"],
        "wikitext_matches": True,
        "conversion": conversion,
        "pdf_ok": pdf_ok,
        "pdf_page_embeds_ok": pdf_embeds_ok,
        "pdf_url": (pdf_info or {}).get("description_url"),
        "images": image_checks,
        "all_images_ok": all(item["ok"] for item in image_checks),
        "statistics": job["statistics"],
        "browser_visual_qa": "pending; inspect the live page in a browser",
    }
    json_write(work / "verification.json", report)
    if not report["all_images_ok"] or not pdf_ok:
        raise PublicationError("Some published media failed hash/dimension verification; see verification.json.")

    job["verified_revision"] = current["revid"]
    job["status"] = "published_api_verified"
    save_job(work, job)
    hooks.emit_fn(
        "api_verified",
        page_url=current["url"],
        revision=current["revid"],
        image_count=len(image_checks),
        pdf_url=report["pdf_url"],
    )
    return report
