"""Small, conservative Action API client for Wikisource and Commons.

Credentials belong to the caller; this module never reads config or prints HTTP
bodies. Use a Wikimedia BotPassword with ``login``. Transport errors after writes
are *unknown outcomes*, not permission to replay the request. Exceptions contain
safe diagnostic codes, not request bodies, cookies, credentials or CSRF tokens.

API references: https://www.mediawiki.org/wiki/API:Edit, API:Upload,
API:Stashimageinfo, API:Imageinfo, API:Allimages, API:Login.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

import requests


WIKISOURCE_API = "https://zh.wikisource.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
DEFAULT_USER_AGENT = (
    "WikisourcePublish/1.0 (local reviewed-publication client; "
    "https://www.mediawiki.org/wiki/API:Etiquette) python-requests"
)


class MediaWikiError(RuntimeError):
    """Known failure. ``code`` is safe for logs; raw responses are not stored."""

    def __init__(self, code: str, message: str = "MediaWiki request failed"):
        self.code = code
        super().__init__(f"{message} [{code}]")


class AuthenticationError(MediaWikiError):
    pass


class ConflictError(MediaWikiError):
    pass


class AmbiguousWriteError(MediaWikiError):
    """Write may have happened. Inspect state before making a new attempt."""

    def __init__(self, code="unknown-outcome", message="Write outcome could not be verified", *, checkpoint=None):
        self.checkpoint = checkpoint
        super().__init__(code, message)


class UploadWarning(MediaWikiError):
    """Upload was stopped for warnings; nothing is automatically overridden."""

    def __init__(self, warnings, *, filekey=None, checkpoint=None):
        self.warnings = warnings
        self.filekey = filekey
        self.checkpoint = checkpoint
        super().__init__("upload-warning", "Upload requires review of server warnings")


def normalize_sha1(value: str) -> str:
    """Normalize API hexadecimal or MediaWiki base36 SHA-1 to 40 hex digits."""
    value = str(value).lower()
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    if not re.fullmatch(r"[0-9a-z]{1,31}", value):
        raise MediaWikiError("invalid-sha1", "Server supplied an invalid SHA-1")
    number = int(value, 36)
    if number >= 1 << 160:
        raise MediaWikiError("invalid-sha1", "Server supplied an invalid SHA-1")
    return f"{number:040x}"


def _edit_text_equal(actual, expected):
    """Ignore only terminal LF characters MediaWiki removes when saving.

    Spaces, CR characters, other Unicode whitespace and all internal line
    endings remain significant. Inaccessible revision content is never equal.
    """
    return isinstance(actual, str) and isinstance(expected, str) and actual.rstrip("\n") == expected.rstrip("\n")


def _file_digest(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        remaining = limit
        while remaining is None or remaining > 0:
            chunk = stream.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    if remaining not in (None, 0):
        raise ConflictError("local-file-changed", "Local upload file changed while reading")
    return digest.hexdigest()


class MediaWikiClient:
    CHUNK_THRESHOLD = 20 * 1024 * 1024
    MIN_CHUNK_SIZE = 5 * 1024 * 1024
    SAFE_ACTIONS = frozenset({"query", "parse", "expandtemplates", "compare", "paraminfo", "help"})

    def __init__(
        self, api_url, username=None, password=None, user_agent=DEFAULT_USER_AGENT,
        *, session=None, timeout=(10, 60), read_retries=3, maxlag=5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        # A typo must not send credentials to an unrelated endpoint; redirects
        # are disabled as well. Add other wikis deliberately, not from user URLs.
        if api_url not in (WIKISOURCE_API, COMMONS_API):
            raise ValueError("Only the HTTPS zh.wikisource.org and commons.wikimedia.org Action API endpoints are supported")
        if not user_agent or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("Provide a descriptive, single-line User-Agent")
        if not 0 <= read_retries <= 5:
            raise ValueError("read_retries must be between 0 and 5")
        self.api_url = api_url
        self.username = username
        self._password = password
        self.user_agent = user_agent
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        if isinstance(self.session, requests.Session):
            # A caller-supplied retry adapter must never replay an upload/edit.
            self.session.mount(self.api_url, requests.adapters.HTTPAdapter(max_retries=0))
        self.timeout = timeout
        self.read_retries = read_retries
        self.maxlag = maxlag
        self._sleep = sleep
        self._csrf = None
        self._authenticated_name = None
        self._secrets = {str(password)} if password else set()
        self.last_warnings = {}

    def _redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(str(key)): self._redact(item) for key, item in value.items()}
        return value

    @staticmethod
    def _delay(response, attempt):
        raw = response.headers.get("Retry-After", "") if response is not None else ""
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            try:
                seconds = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 2 ** attempt
        return min(30.0, max(1.0, seconds))

    def _api_error(self, error):
        # API "info" fields sometimes echo submitted text. Codes are sufficient
        # to identify actionable errors without exposing passwords or tokens.
        code = self._redact(str(error.get("code", "api-error")))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code):
            code = "api-error"
        if code in {"assertuserfailed", "assertnameduserfailed", "assertbotfailed", "notloggedin", "mustbeloggedin", "badtoken"}:
            return AuthenticationError(code, "Authentication or CSRF check failed; authenticate again before retrying")
        if code in {"editconflict", "articleexists", "missingtitle", "pagedeleted", "fileexists-no-change", "fileexists-shared-forbidden"}:
            return ConflictError(code, "Remote state conflicts with the proposed change")
        return MediaWikiError(code)

    def request(self, params: Mapping[str, Any], files=None, post=False):
        """Issue an API request. Only allowlisted read actions receive retries.

        No adapter retries or redirects are used. Mutating requests are sent
        once, including 429/503, malformed JSON and network interruptions.
        ``query`` and ``parse`` may use POST to avoid long URLs and remain reads.
        """
        action = params.get("action", "query")
        safe = action in self.SAFE_ACTIONS and files is None
        if not safe and not post:
            raise ValueError("Mutating API actions require post=True")
        payload = {"action": action, "format": "json", "formatversion": 2,
                   "errorformat": "plaintext", "maxlag": self.maxlag, **params}
        payload["format"] = "json"
        payload["formatversion"] = 2
        payload = {key: ("1" if value is True else value) for key, value in payload.items()
                   if value is not None and value is not False}
        attempts = self.read_retries + 1 if safe else 1
        self.last_warnings = {}
        for attempt in range(attempts):
            response = None
            try:
                kwargs = {"timeout": self.timeout, "allow_redirects": False}
                if post:
                    response = self.session.post(self.api_url, data=payload, files=files, **kwargs)
                else:
                    response = self.session.get(self.api_url, params=payload, **kwargs)
            except requests.RequestException:
                if safe and attempt + 1 < attempts:
                    self._sleep(self._delay(None, attempt))
                    continue
                error = MediaWikiError("transport-error", "Read failed after bounded retries") if safe else AmbiguousWriteError("transport-error")
                raise error from None
            if response.status_code != 200:
                if safe and response.status_code in {429, 502, 503, 504} and attempt + 1 < attempts:
                    self._sleep(self._delay(response, attempt))
                    continue
                code = f"http-{response.status_code}"
                if not safe:
                    raise AmbiguousWriteError(code)
                raise MediaWikiError(code, "API HTTP request failed")
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Expected object")
            except (ValueError, requests.exceptions.JSONDecodeError):
                if safe and attempt + 1 < attempts:
                    self._sleep(self._delay(response, attempt))
                    continue
                if not safe:
                    raise AmbiguousWriteError("invalid-json") from None
                raise MediaWikiError("invalid-json", "API did not return a JSON object") from None
            errors = data.get("errors") or ([data["error"]] if "error" in data else [])
            if errors:
                error = errors[0]
                if safe and error.get("code") in {"maxlag", "ratelimited", "readonly"} and attempt + 1 < attempts:
                    self._sleep(self._delay(response, attempt))
                    continue
                raise self._api_error(error)
            self.last_warnings = self._redact(data.get("warnings", {}))
            return data
        raise AssertionError("Unreachable retry state")

    def _token(self, token_type):
        params = {"action": "query", "meta": "tokens", "type": token_type}
        if token_type == "csrf":
            params.update({"assert": "user", "assertuser": self._authenticated_name})
        token = self.request(params).get("query", {}).get("tokens", {}).get(token_type + "token")
        if not isinstance(token, str) or not token or token == "+\\":
            raise AuthenticationError("missing-token", "Authenticated API token is unavailable")
        self._secrets.add(token)
        return token

    def login(self):
        """Authenticate with a BotPassword; return the verified userinfo dict."""
        if not self.username or not self._password:
            raise AuthenticationError("missing-credentials", "Username and BotPassword are required")
        self._csrf = None
        self._authenticated_name = None
        token = self._token("login")
        result = self.request({"action": "login", "lgname": self.username,
                               "lgpassword": self._password, "lgtoken": token}, post=True).get("login", {})
        if result.get("result") != "Success":
            raise AuthenticationError("login-failed", "BotPassword login was not accepted")
        self._authenticated_name = result.get("lgusername")
        return self._ensure_auth()

    def userinfo(self):
        info = self.request({"action": "query", "meta": "userinfo", "uiprop": "rights|groups"}).get("query", {}).get("userinfo")
        if not isinstance(info, dict):
            raise MediaWikiError("missing-userinfo", "User identity response is incomplete")
        return info

    def _ensure_auth(self):
        info = self.userinfo()
        if "anon" in info or "temp" in info or not info.get("id") or not info.get("name"):
            raise AuthenticationError("notloggedin", "A named authenticated account is required")
        expected = (self.username.split("@", 1)[0] if self.username else None) or self._authenticated_name
        def normalized_name(name):
            # MediaWiki uppercases the first character; the rest remains case
            # sensitive. A casefold could silently accept a different account.
            name = name.replace("_", " ").strip()
            return name[:1].upper() + name[1:]
        if expected and normalized_name(info["name"]) != normalized_name(expected):
            raise AuthenticationError("wrong-user", "Authenticated account differs from the configured account")
        self._authenticated_name = info["name"]
        return info

    def _write_params(self):
        if not self._csrf:
            self._csrf = self._token("csrf")
        return {"assert": "user", "assertuser": self._authenticated_name, "token": self._csrf}

    def _url(self, title):
        parts = urlsplit(self.api_url)
        return f"{parts.scheme}://{parts.netloc}/wiki/{quote(title.replace(' ', '_'), safe='/:')}"

    @staticmethod
    def _pages(data):
        pages = data.get("query", {}).get("pages", [])
        return list(pages.values()) if isinstance(pages, dict) else pages

    def page(self, title):
        data = self.request({"action": "query", "titles": title, "prop": "info|revisions",
                             "inprop": "url", "rvprop": "ids|timestamp|content", "rvslots": "main", "rvlimit": 1})
        pages = self._pages(data)
        if len(pages) != 1 or "invalid" in pages[0]:
            raise MediaWikiError("invalid-title", "Expected one valid page title")
        page = pages[0]
        missing = "missing" in page
        revision = (page.get("revisions") or [{}])[0]
        slot = revision.get("slots", {}).get("main", revision)
        content = "" if missing else slot.get("content", slot.get("*"))
        return {"title": page["title"], "pageid": page.get("pageid"), "missing": missing,
                "revid": revision.get("revid"), "timestamp": revision.get("timestamp"),
                "content": content, "url": page.get("fullurl") or self._url(page["title"])}

    @staticmethod
    def _file_title(title):
        title = str(title).strip().replace("_", " ")
        for prefix in ("File:", "Image:", "檔案:", "文件:", "图像:", "圖像:"):
            if title.casefold().startswith(prefix.casefold()):
                title = title[len(prefix):]
                break
        if not title or "|" in title or "\n" in title or "\r" in title:
            raise ValueError("Expected a single nonempty file title")
        return "File:" + title

    def _normalize_file(self, info, title=None):
        canonical = info.get("canonicaltitle") or info.get("title") or title
        if not canonical:
            canonical = self._file_title(info["name"])
        canonical = self._file_title(canonical)
        return {"title": canonical, "canonical_title": canonical, "name": canonical.split(":", 1)[1],
                "url": info.get("url"), "description_url": info.get("descriptionurl") or self._url(canonical),
                "sha1": normalize_sha1(info["sha1"]) if info.get("sha1") else None,
                "width": info.get("width"), "height": info.get("height"), "size": info.get("size"),
                "extmetadata": info.get("extmetadata", {}), "mime": info.get("mime"),
                "timestamp": info.get("timestamp")}

    def file_info(self, title):
        data = self.request({"action": "query", "titles": self._file_title(title), "prop": "imageinfo",
                             "iiprop": "canonicaltitle|url|sha1|size|mime|extmetadata|timestamp", "iilimit": 1})
        pages = self._pages(data)
        if len(pages) != 1 or "invalid" in pages[0]:
            raise MediaWikiError("invalid-file-title", "Expected one valid file title")
        page = pages[0]
        if not page.get("imageinfo"):
            return None
        # Shared Commons images may carry 'missing' on Wikisource, but their
        # imageinfo remains real and must not be discarded.
        return self._normalize_file(page["imageinfo"][0], page["title"])

    def find_files_by_sha1(self, hexsha1):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", hexsha1):
            raise ValueError("Expected a 40-character hexadecimal SHA-1")
        params = {"action": "query", "list": "allimages", "aisha1": hexsha1.lower(), "ailimit": 50,
                  "aiprop": "canonicaltitle|url|sha1|size|mime|extmetadata|timestamp"}
        result = []
        seen = set()
        for _ in range(100):
            data = self.request(params)
            for item in data.get("query", {}).get("allimages", []):
                info = self._normalize_file(item)
                if info["sha1"] != hexsha1.lower():
                    raise MediaWikiError("sha1-query-mismatch", "Hash lookup returned a different file")
                result.append(info)
            continuation = data.get("continue")
            if not continuation:
                return result
            key = tuple(sorted(continuation.items()))
            if key in seen:
                raise MediaWikiError("repeated-continuation", "Hash query continuation did not advance")
            seen.add(key)
            params.update(continuation)
        raise MediaWikiError("continuation-limit", "Hash query exceeded the bounded page limit")

    def parse(self, wikitext, title):
        # uselang governs UI messages, not body conversion. Request the base
        # Chinese variant and disable conversion in this preview's source too.
        # This magic word is preview-only; the caller owns the published text.
        source = "__NOCONTENTCONVERT__\n" + wikitext
        data = self.request({"action": "parse", "text": source, "title": title,
                             "contentmodel": "wikitext", "prop": "text|images|categories|links|displaytitle|parsewarnings",
                             "disableeditsection": True, "disabletoc": True, "disablelimitreport": True,
                             "uselang": "zh", "variant": "zh"}, post=True)
        parsed = data.get("parse")
        if not isinstance(parsed, dict):
            raise MediaWikiError("missing-parse", "Parser response is incomplete")
        html = parsed.get("text", "")
        if isinstance(html, dict):
            html = html.get("*", "")
        return {"html": html, "images": parsed.get("images", []), "title": parsed.get("title", title),
                "displaytitle": parsed.get("displaytitle"), "categories": parsed.get("categories", []),
                "links": parsed.get("links", []), "warnings": self._redact(parsed.get("parsewarnings", [])),
                "api_warnings": self._redact(data.get("warnings", {}))}

    @staticmethod
    def _edit_result(page, status, old_revid=None, warnings=None):
        return {"status": status, "title": page["title"], "pageid": page["pageid"],
                "revid": page["revid"], "old_revid": old_revid, "timestamp": page["timestamp"],
                "url": page["url"], "recovered": status == "recovered", "warnings": warnings or {}}

    def edit(self, title, text, summary, base_revision=None, base_timestamp=None, create_only=False):
        """Full-page edit guarded by revision and existence, never blind retry.

        ``create_only=False`` means *update only*, never implicit creation.
        Omitted base_revision is taken from the preflight read. A timeout is
        considered recovered only if a new read matches ``text``, allowing
        terminal LF characters to be removed by MediaWiki when saving.
        """
        self._ensure_auth()
        before = self.page(title)
        if not before["missing"] and _edit_text_equal(before["content"], text):
            return self._edit_result(before, "noop", before["revid"])
        if create_only and not before["missing"]:
            raise ConflictError("articleexists", "Create-only target already exists with different text")
        if not create_only and before["missing"]:
            raise ConflictError("missingtitle", "Update-only target does not exist")
        if not before["missing"] and (before["content"] is None or before["revid"] is None):
            raise ConflictError("unreadable-base", "Cannot safely replace an unreadable revision")
        if base_revision is not None and before["revid"] != base_revision:
            raise ConflictError("editconflict", "Page revision changed since the proposal was prepared")
        if base_timestamp is not None and before["timestamp"] != base_timestamp:
            raise ConflictError("editconflict", "Page timestamp changed since the proposal was prepared")
        params = {"action": "edit", "title": before["title"], "text": text, "summary": summary,
                  "watchlist": "nochange", **self._write_params()}
        if create_only:
            params["createonly"] = True
        else:
            params.update({"nocreate": True, "baserevid": before["revid"]})
            if base_timestamp is not None:
                params["basetimestamp"] = base_timestamp
        try:
            data = self.request(params, post=True)
            result = data.get("edit", {})
            if result.get("result") != "Success":
                if result.get("captcha"):
                    raise MediaWikiError("captcha-required", "Edit requires an interactive CAPTCHA")
                if result.get("code"):
                    raise self._api_error(result)
                raise AmbiguousWriteError("incomplete-edit-response")
            if not result.get("newrevid") and "nochange" not in result:
                raise AmbiguousWriteError("incomplete-edit-response")
        except AmbiguousWriteError:
            try:
                after = self.page(before["title"])
            except MediaWikiError:
                raise AmbiguousWriteError("edit-readback-failed") from None
            if not after["missing"] and _edit_text_equal(after["content"], text):
                return self._edit_result(after, "recovered", before["revid"])
            raise AmbiguousWriteError("edit-not-confirmed", "Edit outcome differs from expected text; inspect page history before retrying") from None
        page = {"title": result.get("title", before["title"]), "pageid": result.get("pageid", before["pageid"]),
                "revid": result.get("newrevid", before["revid"]),
                "timestamp": result.get("newtimestamp", before["timestamp"]), "url": before["url"]}
        status = "noop" if "nochange" in result else ("created" if before["missing"] else "edited")
        return self._edit_result(page, status, before["revid"], self._redact(data.get("warnings", {})))

    @staticmethod
    def _upload_result(info, status, warnings=None):
        return {"status": status, "file": info, "warnings": warnings or {}, "recovered": status == "recovered"}

    def _upload_readback(self, filename, sha1, *, status, warnings=None, checkpoint=None):
        try:
            info = self.file_info(filename)
        except MediaWikiError:
            raise AmbiguousWriteError("upload-readback-failed", checkpoint=checkpoint) from None
        if info is None or info["sha1"] != sha1:
            raise AmbiguousWriteError("upload-not-confirmed", "Uploaded hash is not confirmed; inspect remote state before retrying", checkpoint=checkpoint)
        return self._upload_result(info, status, warnings)

    def _stash_offset(self, filekey, path, filesize):
        data = self.request({"action": "query", "prop": "stashimageinfo", "siifilekey": filekey,
                             "siiprop": "size|sha1", "assert": "user", "assertuser": self._authenticated_name})
        items = data.get("query", {}).get("stashimageinfo", [])
        if isinstance(items, dict):
            items = [items] if "size" in items else list(items.values())
        if len(items) != 1:
            raise ConflictError("stash-unavailable", "Upload checkpoint stash is missing or ambiguous")
        item = items[0]
        offset = item.get("size")
        if not isinstance(offset, int) or not 0 <= offset <= filesize or not item.get("sha1"):
            raise ConflictError("invalid-stash", "Stashed upload offset or hash is unavailable")
        if normalize_sha1(item["sha1"]) != _file_digest(path, offset):
            raise ConflictError("stash-hash-mismatch", "Stashed bytes differ from the local file prefix")
        return offset

    def upload(self, path, filename, description, comment, *, checkpoint=None, on_checkpoint=None, chunk_size=5 * 1024 * 1024):
        """Upload bytes, reusing any same-hash file and refusing overwrites.

        Files >=20 MiB use a stash, then a separate commit. The optional callback
        receives JSON-serializable checkpoints after each acknowledged chunk.
        Store these privately: filekeys are session-bound upload handles. Resume
        by passing one back; the remote prefix hash and size are verified first.
        Unknown chunk responses stop with a checkpoint, without replaying bytes.
        """
        path = Path(path)
        filesize = path.stat().st_size
        if not path.is_file() or filesize <= 0 or filesize > 5_368_709_120:
            raise ValueError("Upload must be a nonempty regular file of at most 5 GiB")
        if chunk_size < self.MIN_CHUNK_SIZE:
            raise ValueError("chunk_size must be at least 5 MiB")
        filename = self._file_title(filename).split(":", 1)[1]
        sha1 = _file_digest(path)
        self._ensure_auth()
        existing = self.file_info(filename)
        if existing:
            if existing["sha1"] != sha1:
                raise ConflictError("filename-hash-conflict", "A different file already uses this name; overwriting is disabled")
            return self._upload_result(existing, "reused")
        duplicates = self.find_files_by_sha1(sha1)
        if duplicates:
            return self._upload_result(duplicates[0], "reused")
        state = {"schema": 1, "api_url": self.api_url, "filename": filename, "sha1": sha1,
                 "filesize": filesize, "filekey": None, "offset": 0, "complete": False}
        if checkpoint:
            for key in ("schema", "api_url", "filename", "sha1", "filesize"):
                if checkpoint.get(key) != state[key]:
                    raise ConflictError("checkpoint-mismatch", "Checkpoint belongs to a different upload")
            if not checkpoint.get("filekey"):
                raise ConflictError("checkpoint-no-filekey", "Cannot resume an unacknowledged first chunk; inspect the upload stash")
            state["filekey"] = checkpoint["filekey"]
            state["offset"] = self._stash_offset(state["filekey"], path, filesize)
        params = {"action": "upload", "filename": filename, "text": description, "comment": comment,
                  "watchlist": "nochange", **self._write_params()}

        def save_state():
            if on_checkpoint:
                on_checkpoint(dict(state))

        if filesize >= self.CHUNK_THRESHOLD or checkpoint:
            save_state()
            with path.open("rb") as stream:
                stream.seek(state["offset"])
                while state["offset"] < filesize:
                    chunk = stream.read(min(chunk_size, filesize - state["offset"]))
                    if not chunk:
                        raise ConflictError("local-file-changed", "Local upload file changed while reading")
                    chunk_params = {**params, "stash": True, "filesize": filesize,
                                    "offset": state["offset"], "filekey": state["filekey"]}
                    try:
                        response = self.request(chunk_params, files={"chunk": (filename, chunk, "application/octet-stream")}, post=True)
                        result = response.get("upload", {})
                        if result.get("warnings") or result.get("result") == "Warning":
                            raise UploadWarning(self._redact(result.get("warnings", {})), filekey=result.get("filekey"), checkpoint=dict(state))
                        if result.get("result") not in {"Continue", "Success"} or not result.get("filekey"):
                            raise AmbiguousWriteError("incomplete-chunk-response")
                        expected = state["offset"] + len(chunk)
                        offset = result.get("offset", filesize if result.get("result") == "Success" else None)
                        if offset != expected:
                            raise AmbiguousWriteError("unexpected-chunk-offset")
                        state.update(filekey=result["filekey"], offset=offset)
                        save_state()
                    except AmbiguousWriteError:
                        # Verify a known stash without resending the uncertain
                        # chunk. Caller may explicitly resume from this state.
                        if state["filekey"]:
                            try:
                                state["offset"] = self._stash_offset(state["filekey"], path, filesize)
                            except MediaWikiError:
                                pass
                        save_state()
                        raise AmbiguousWriteError("chunk-not-confirmed", "Chunk interrupted; inspect checkpoint and explicitly resume", checkpoint=dict(state)) from None
            if _file_digest(path) != sha1 or path.stat().st_size != filesize:
                raise ConflictError("local-file-changed", "Local file changed before upload commit")
            params["filekey"] = state["filekey"]
            files = None
        else:
            # requests builds multipart bodies in memory; keeping this below
            # the threshold bounds memory and also closes the source promptly.
            blob = path.read_bytes()
            if hashlib.sha1(blob).hexdigest() != sha1 or len(blob) != filesize:
                raise ConflictError("local-file-changed", "Local file changed before upload")
            files = {"file": (filename, blob, "application/octet-stream")}
        try:
            data = self.request(params, files=files, post=True)
            result = data.get("upload", {})
            if result.get("warnings") or result.get("result") == "Warning":
                raise UploadWarning(self._redact(result.get("warnings", {})), filekey=result.get("filekey"), checkpoint=dict(state))
            if result.get("result") != "Success":
                raise AmbiguousWriteError("incomplete-upload-response")
        except AmbiguousWriteError:
            recovered = self._upload_readback(filename, sha1, status="recovered", checkpoint=dict(state))
            state.update(complete=True, offset=filesize)
            save_state()
            return recovered
        uploaded = self._upload_readback(result.get("filename", filename), sha1, status="uploaded",
                                         warnings=self._redact(data.get("warnings", {})), checkpoint=dict(state))
        state.update(complete=True, offset=filesize)
        save_state()
        return uploaded
