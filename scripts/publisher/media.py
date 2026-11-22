"""Commons PDF resolution, PDF page-map verification and image asset preparation/upload."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from PIL import Image
import requests

from md_wikitext import collect_images
from publisher.models import (
    PublicationError,
    checked_local,
    digest,
    file_record,
    filename,
    save_job,
)


def commons_name(value: str) -> str | None:
    """Resolve an upload/wiki Commons URL or ``File:`` title to its canonical Commons name."""
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        if (
            parsed.hostname == "upload.wikimedia.org"
            and "/wikipedia/commons/" in parsed.path
            and "/thumb/" not in parsed.path
        ):
            return unquote(parsed.path.rsplit("/", 1)[-1]).replace("_", " ")
        if parsed.hostname == "commons.wikimedia.org" and "/wiki/File:" in parsed.path:
            return unquote(parsed.path.split("/wiki/File:", 1)[1]).replace("_", " ")
        return None
    if value.startswith("File:"):
        return value[5:].replace("_", " ")
    return None


def resolve_pdf(value: str, work: Path, commons, title: str, metadata: dict) -> dict:
    """Verify an existing Commons PDF or download/validate a local PDF against Commons SHA-1."""
    name = commons_name(value)
    if name:
        info = commons.file_info(name)
        if not info or info.get("mime") != "application/pdf":
            raise PublicationError("The specified Commons PDF does not exist or is not a PDF.")
        return {
            "input": value,
            "filename": info["name"],
            "remote": info,
            "sha1": info["sha1"],
            "status": "reused",
        }
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        path = work / "source.pdf"
        response = requests.get(
            value,
            stream=True,
            timeout=(15, 60),
            headers={"User-Agent": "WikisourcePublish/1.0 (user-directed book publishing)"},
        )
        response.raise_for_status()
        with path.open("wb") as stream:
            for block in response.iter_content(1024 * 1024):
                stream.write(block)
    else:
        path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise PublicationError(f"PDF not found: {path}")
    with path.open("rb") as stream:
        if not stream.read(1024).lstrip().startswith(b"%PDF-"):
            raise PublicationError("Source is not a PDF (missing PDF signature).")
    result = file_record(path)
    requested = metadata.get("pdf_filename", title + " - " + result["sha1"][:10])
    if requested.lower().endswith(".pdf"):
        requested = requested[:-4]
    result.update(input=value, filename=filename(requested) + ".pdf", status="pending")
    matches = commons.find_files_by_sha1(result["sha1"])
    if matches:
        info = matches[0]
        result.update(filename=info["name"], remote=info, status="reused")
    return result


def read_pdf_page_map(path: str | Path, targets, pdf: dict) -> dict:
    """Validate an explicit, reviewed local-image-to-PDF-page map without inferring from names."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pages = data.get("image_pages") if isinstance(data, dict) else None
    if not isinstance(pages, dict) or not pages:
        raise PublicationError("PDF page map must contain a nonempty image_pages object.")
    if pdf.get("status") != "reused" or not pdf.get("filename", "").lower().endswith(".pdf"):
        raise PublicationError("PDF page reuse requires an already existing Commons PDF.")
    if set(pages) - set(targets):
        raise PublicationError("PDF page map refers to images absent from the Markdown.")
    if any(type(page) is not int or page < 1 for page in pages.values()):
        raise PublicationError("PDF page numbers must be positive integers.")
    evidence = data.get("evidence", {})
    count = evidence.get("source_pdf_pages")
    if count is not None and (type(count) is not int or count < max(pages.values())):
        raise PublicationError("Mapped page exceeds the recorded source PDF page count.")
    if evidence.get("source_pdf"):
        source = Path(evidence["source_pdf"])
        if digest(source, "sha1") != pdf["sha1"]:
            raise PublicationError("PDF page-map evidence refers to a different source PDF.")
        if evidence.get("source_pdf_sha256"):
            checked_local(source, evidence["source_pdf_sha256"])
    return pages


def collect_and_prepare_images(
    body: str,
    md_path: Path,
    title: str,
    metadata: dict,
    pdf: dict,
    previous_images: list[dict],
    page_map: dict,
) -> list[dict]:
    """Gather referenced images, validate scan page metadata, read dimensions and resolve reuse."""
    collected = collect_images(body, md_path)
    scan_pages = metadata.get("image_scan_pages", {})
    if (
        not isinstance(scan_pages, dict)
        or set(scan_pages) - {im["target"] for im in collected}
        or any(type(page) is not int or page < 1 for page in scan_pages.values())
    ):
        raise PublicationError(
            "metadata.image_scan_pages must map referenced image targets to positive scan page numbers."
        )
    images = []
    seen = set()
    previous = {im["target"]: im for im in previous_images}
    for item in collected:
        if item["target"] in seen:
            continue
        seen.add(item["target"])
        path = Path(item["path"])
        if not path.is_file():
            raise PublicationError(f"Missing referenced image: {path}")
        record = {**item, **file_record(path)}
        with Image.open(path) as img:
            record.update(width=img.width, height=img.height)
        name = filename(title + " - " + path.stem + " - " + record["sha1"][:10]) + path.suffix.lower()
        page_match = re.search(r"(?:scan|page|portrait|full|p)[-_]?(\d{1,4})", path.stem, re.I)
        record.update(
            requested_filename=name,
            scan_page=scan_pages.get(item["target"], int(page_match.group(1)) if page_match else None),
            status="pending",
        )
        prior = previous.get(item["target"])
        if item["target"] in page_map:
            record.update(
                source_kind="pdf_page",
                pdf_page=page_map[item["target"]],
                source_pdf_sha1=pdf["sha1"],
                commons_filename=pdf["filename"],
                status="reused_pdf_page",
            )
        elif (
            prior
            and prior.get("source_kind") != "pdf_page"
            and prior.get("sha1") == record["sha1"]
            and prior.get("commons_filename")
        ):
            record.update(commons_filename=prior["commons_filename"], status="recorded")
        images.append(record)
    return images


def image_mapping(job: dict) -> dict:
    """Build the target-to-Commons-embed mapping passed into ``md_wikitext.convert``."""
    mapping = {}
    for image in job["images"]:
        if image.get("source_kind") == "pdf_page":
            page = image.get("pdf_page")
            if type(page) is not int or page < 1 or image.get("source_pdf_sha1") != job["pdf"]["sha1"]:
                raise PublicationError("Invalid PDF page mapping or changed source PDF hash.")
            mapping[image["target"]] = {"filename": job["pdf"]["filename"], "page": page}
        else:
            mapping[image["target"]] = image.get("commons_filename", image["requested_filename"])
    return mapping


def ensure_remote_asset(commons, record: dict, requested: str, desc: str, job: dict, work: Path) -> dict:
    """Upload or reuse a single file on Commons with checkpoint persistence and SHA-1/dimension checks."""
    checked_local(record["path"], record["sha256"])
    checkpoint = record.get("upload_checkpoint")

    def progress(value):
        record["upload_checkpoint"] = value
        save_job(work, job)

    result = commons.upload(
        record["path"],
        requested,
        desc,
        "Publish scanned source illustration for " + job["title"],
        checkpoint=checkpoint,
        on_checkpoint=progress,
    )
    info = result["file"]
    if info["sha1"] != record["sha1"]:
        raise PublicationError("Uploaded file hash does not match the source image/PDF.")
    if "width" in record and (info.get("width"), info.get("height")) != (record["width"], record["height"]):
        raise PublicationError("Uploaded image dimensions differ from the local source.")
    record.update(commons_filename=info["name"], remote=info, status=result["status"])
    save_job(work, job)
    return info
