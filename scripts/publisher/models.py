"""Core shared types, IO helpers, credential handling and client factory."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat

from mediawiki_client import MediaWikiClient

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SOURCE_API = "https://zh.wikisource.org/w/api.php"
CREDENTIALS = Path.home() / ".config/wikisource-publish/credentials.json"


class PublicationError(Exception):
    """Domain validation, integrity or workflow precondition failure."""


def now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def digest(path: str | Path, algorithm: str = "sha256") -> str:
    """Compute a streaming hexadecimal digest for a local file."""
    hasher = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def json_write(path: str | Path, value) -> None:
    """Atomically write UTF-8 JSON with a trailing newline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


def emit(event: str, **data) -> None:
    """Emit a single structured JSON event line to stdout."""
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def filename(value: str) -> str:
    """Normalize a Wikisource title/stem into a filesystem- and Commons-safe filename."""
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*#\[\]{}]', "-", value).strip(" .")
    while len(cleaned.encode("utf-8")) > 220:
        cleaned = cleaned[:-1]
    if not cleaned:
        raise PublicationError("Empty file/title after normalization")
    return cleaned


def text_parameter(value) -> str:
    """Escape template parameter delimiters while preserving square-bracket wikilinks."""
    return (
        html.escape(str(value), quote=False)
        .replace("|", "&#124;")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
    )


def publication_equal(left: str, right: str) -> bool:
    """Compare stored wikitext ignoring only MediaWiki trailing LF normalization."""
    return left.rstrip("\n") == right.rstrip("\n")


def checked_local(path: str | Path, expected_sha256: str) -> None:
    """Ensure a local file exists and still matches its recorded SHA-256 digest."""
    target = Path(path)
    if not target.is_file() or digest(target) != expected_sha256:
        raise PublicationError(
            f"Input changed or disappeared since preparation: {target}. Prepare again."
        )


def file_record(path: str | Path) -> dict:
    """Build a canonical resolved-path, hash and byte-size record for a local file."""
    resolved = Path(path).resolve()
    return {
        "path": str(resolved),
        "sha256": digest(resolved),
        "sha1": digest(resolved, "sha1"),
        "size": resolved.stat().st_size,
    }


def save_job(work: Path, job: dict) -> None:
    """Persist ``job.json`` atomically in the package work directory."""
    json_write(work / "job.json", job)


def credentials(path: str | Path) -> dict:
    """Load owner-only (0600) Wikimedia BotPassword credentials without leaking secrets."""
    cred_path = Path(path).expanduser()
    if not cred_path.exists():
        raise PublicationError(
            "Credentials are not configured. Run configure_credentials.py in your own terminal first."
        )
    if stat.S_IMODE(cred_path.stat().st_mode) & 0o077:
        raise PublicationError("Credential file must be readable only by its owner (chmod 600).")
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    if not data.get("username") or not data.get("password"):
        raise PublicationError("Credential file is missing username or password; no secret was printed.")
    return data


def clients(path=None, *, credentials_fn=credentials) -> tuple[MediaWikiClient, MediaWikiClient]:
    """Instantiate paired Commons and Chinese Wikisource API clients."""
    data = credentials_fn(path) if path else {}
    return (MediaWikiClient(COMMONS_API, **data), MediaWikiClient(SOURCE_API, **data))


def first_difference(left: str, right: str) -> dict:
    """Return the first differing offset and nearby snippet between two strings."""
    end = min(len(left), len(right))
    for index in range(end):
        if left[index] != right[index]:
            return {
                "offset": index,
                "expected": left[max(0, index - 30) : index + 80],
                "actual": right[max(0, index - 30) : index + 80],
            }
    return {
        "offset": end,
        "expected": left[max(0, end - 30) : end + 80],
        "actual": right[max(0, end - 30) : end + 80],
    }
