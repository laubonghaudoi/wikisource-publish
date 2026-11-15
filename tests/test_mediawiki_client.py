"""Transport and publication safeguards; all HTTP responses are simulated."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mediawiki_client import (  # noqa: E402
    AmbiguousWriteError, AuthenticationError, COMMONS_API, ConflictError,
    MediaWikiClient, MediaWikiError, UploadWarning, WIKISOURCE_API, normalize_sha1,
)


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("Unexpected additional network request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, Response) else Response(response)

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


def account(name="Editor", **extra):
    return {"query": {"userinfo": {"id": 42, "name": name, **extra}}}


def page(content="old", revision=17, title="Page:Book.pdf/1", missing=False):
    item = {"title": title}
    if missing:
        item["missing"] = True
    else:
        item.update(pageid=23, revisions=[{"revid": revision, "timestamp": "2026-01-01T00:00:00Z",
                                         "slots": {"main": {"content": content}}}])
    return {"query": {"pages": [item]}}


def fileinfo(data, name="Test.png"):
    return {"canonicaltitle": "File:" + name, "url": "https://upload.wikimedia.org/example.png",
            "sha1": hashlib.sha1(data).hexdigest(), "size": len(data), "width": 25, "height": 20,
            "mime": "image/png", "extmetadata": {"LicenseShortName": {"value": "Public domain"}}}


def file_response(data=None, name="Test.png", **extra):
    item = {"title": "File:" + name, **extra}
    if data is None:
        item["missing"] = True
    else:
        item["imageinfo"] = [fileinfo(data, name)]
    return {"query": {"pages": [item]}}


TOKEN = {"query": {"tokens": {"csrftoken": "CSRF-SECRET"}}}
EMPTY_HASHES = {"query": {"allimages": []}}


class ClientCase(unittest.TestCase):
    def client(self, *responses, **kwargs):
        session = Session(*responses)
        sleeps = []
        client = MediaWikiClient(WIKISOURCE_API, session=session, sleep=sleeps.append, **kwargs)
        return client, session, sleeps


class ClientTests(ClientCase):
    def test_endpoint_and_agent_validation(self):
        for endpoint in ("http://zh.wikisource.org/w/api.php", "https://example.org/w/api.php",
                         WIKISOURCE_API + "?redirect=evil"):
            with self.assertRaises(ValueError):
                MediaWikiClient(endpoint)
        with self.assertRaises(ValueError):
            MediaWikiClient(COMMONS_API, user_agent="bad\nheader")

    def test_safe_read_retries_and_defaults(self):
        client, session, sleeps = self.client(Response({}, 429, {"Retry-After": "1000"}),
                                              Response({}, 503), {"query": {"ok": True}})
        self.assertEqual(client.request({"action": "query"}), {"query": {"ok": True}})
        self.assertEqual(sleeps, [30.0, 2.0])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0]["params"]["maxlag"], 5)
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertEqual(session.calls[0]["timeout"], (10, 60))
        self.assertIn("WikisourcePublish", session.headers["User-Agent"])

    def test_read_retries_exhausted_without_leaking_exception(self):
        client, session, _ = self.client(*[requests.Timeout("SECRET password") for _ in range(3)], read_retries=2)
        with self.assertRaises(MediaWikiError) as caught:
            client.request({"action": "query"})
        self.assertEqual(len(session.calls), 3)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_post_parse_read_retries_maxlag(self):
        client, session, _ = self.client({"error": {"code": "maxlag"}}, {"parse": {"text": "ok"}})
        self.assertEqual(client.parse("body", "Test")["html"], "ok")
        self.assertEqual([call["method"] for call in session.calls], ["POST", "POST"])

    def test_writes_never_retry_even_rate_limit_or_invalid_response(self):
        for response in (Response({}, 503), Response({}, 429), requests.Timeout("private"),
                         Response(ValueError("private body")), Response([], 200)):
            client, session, sleeps = self.client(response)
            with self.assertRaises(AmbiguousWriteError):
                client.request({"action": "edit", "token": "private"}, post=True)
            self.assertEqual(len(session.calls), 1)
            self.assertFalse(sleeps)

    def test_known_mutation_error_is_not_replayed(self):
        client, session, _ = self.client({"error": {"code": "editconflict", "info": "password secret"}})
        with self.assertRaises(ConflictError) as caught:
            client.request({"action": "edit"}, post=True)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(session.calls), 1)

    def test_redirects_and_mutating_get_are_blocked(self):
        client, session, _ = self.client(Response({}, 302, {"Location": "https://evil.example"}))
        with self.assertRaises(ValueError):
            client.request({"action": "login"})
        with self.assertRaises(MediaWikiError):
            client.request({"action": "query"})
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0]["allow_redirects"])

    def test_existing_retry_adapter_is_disabled_for_this_endpoint(self):
        session = requests.Session()
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=4))
        MediaWikiClient(WIKISOURCE_API, session=session)
        self.assertEqual(session.get_adapter(WIKISOURCE_API).max_retries.total, 0)

    def test_page_normalization_and_missing(self):
        client, _, _ = self.client(page(content="粤粵淸清"), page(missing=True))
        info = client.page("Page:Book.pdf/1")
        self.assertEqual(set(info), {"title", "pageid", "missing", "revid", "timestamp", "content", "url"})
        self.assertEqual(info["content"], "粤粵淸清")
        self.assertEqual(info["revid"], 17)
        missing = client.page("missing")
        self.assertTrue(missing["missing"])
        self.assertEqual(missing["content"], "")
        self.assertIsNone(missing["revid"])

    def test_hidden_content_is_not_treated_as_empty_text(self):
        data = page()
        data["query"]["pages"][0]["revisions"][0]["slots"]["main"] = {"texthidden": True}
        client, _, _ = self.client(data)
        self.assertIsNone(client.page("Test")["content"])

    def test_sha1_hex_base36_and_invalid(self):
        self.assertEqual(normalize_sha1("ABCDEF" + "0" * 34), "abcdef" + "0" * 34)
        self.assertEqual(normalize_sha1("z"), "0" * 38 + "23")
        self.assertEqual(normalize_sha1("0"), "0" * 40)
        for value in ("!", "z" * 31, "a" * 39):
            with self.assertRaises(MediaWikiError):
                normalize_sha1(value)

    def test_shared_imageinfo_and_absent_file(self):
        client, _, _ = self.client(file_response(b"bytes", missing=True), file_response())
        info = client.file_info("檔案:Test.png")
        self.assertEqual(info["canonical_title"], "File:Test.png")
        self.assertEqual(info["name"], "Test.png")
        self.assertEqual(info["size"], 5)
        self.assertEqual(info["sha1"], hashlib.sha1(b"bytes").hexdigest())
        self.assertIsNone(client.file_info("absent.png"))

    def test_hash_search_continuation_and_hash_validation(self):
        info = fileinfo(b"bytes")
        client, session, _ = self.client({"query": {"allimages": [info]}, "continue": {"aicontinue": "next", "continue": "-||"}},
                                         EMPTY_HASHES)
        self.assertEqual(len(client.find_files_by_sha1(info["sha1"])), 1)
        self.assertEqual(session.calls[1]["params"]["aicontinue"], "next")
        client, _, _ = self.client({"query": {"allimages": [info]}})
        with self.assertRaises(MediaWikiError):
            client.find_files_by_sha1("0" * 40)

    def test_parse_disables_conversion_and_page_furniture(self):
        text = "粤粵淸清决決吿告"
        client, session, _ = self.client({"parse": {"text": {"*": "<p>" + text + "</p>"}, "images": ["Map.png"],
                                                    "parsewarnings": ["check template"]}})
        result = client.parse(text, "Test")
        data = session.calls[0]["data"]
        self.assertEqual(data["text"], "__NOCONTENTCONVERT__\n" + text)
        self.assertEqual(data["variant"], "zh")
        self.assertEqual(data["uselang"], "zh")
        for flag in ("disableeditsection", "disabletoc", "disablelimitreport"):
            self.assertEqual(data[flag], "1")
        self.assertEqual(result["html"], "<p>" + text + "</p>")
        self.assertEqual(result["images"], ["Map.png"])
        self.assertEqual(result["warnings"], ["check template"])

    def test_login_botpassword_and_identity(self):
        client, session, _ = self.client({"query": {"tokens": {"logintoken": "LOGIN-SECRET"}}},
                                         {"login": {"result": "Success", "lgusername": "Editor"}}, account(),
                                         username="Editor@publisher", password="PASSWORD-SECRET")
        self.assertEqual(client.login()["name"], "Editor")
        self.assertEqual(session.calls[1]["data"]["lgtoken"], "LOGIN-SECRET")
        self.assertEqual(session.calls[1]["data"]["lgpassword"], "PASSWORD-SECRET")
        redacted = client._redact({"PASSWORD-SECRET": "LOGIN-SECRET PASSWORD-SECRET"})
        self.assertEqual(redacted, {"[REDACTED]": "[REDACTED] [REDACTED]"})

    def test_login_rejects_wrong_account_and_missing_credentials(self):
        client, session, _ = self.client()
        with self.assertRaises(AuthenticationError):
            client.login()
        self.assertFalse(session.calls)
        client, _, _ = self.client({"query": {"tokens": {"logintoken": "TOKEN"}}},
                                   {"login": {"result": "Success", "lgusername": "Other"}}, account("Other"),
                                   username="Editor@bot", password="secret")
        with self.assertRaises(AuthenticationError):
            client.login()

    def test_anonymous_and_temporary_accounts_cannot_write(self):
        for info in (account(anon=True), account(temp=True), {"query": {"userinfo": {"id": 0}}}):
            client, session, _ = self.client(info)
            with self.assertRaises(AuthenticationError):
                client.edit("Test", "text", "summary")
            self.assertEqual(len(session.calls), 1)

    def test_account_matching_preserves_case_after_first_character(self):
        client, _, _ = self.client(account("EditorName"), username="editorName@bot")
        self.assertEqual(client._ensure_auth()["name"], "EditorName")
        client, _, _ = self.client(account("Editorname"), username="EditorName@bot")
        with self.assertRaises(AuthenticationError):
            client._ensure_auth()

    def test_edit_noop_never_fetches_token_or_posts(self):
        client, session, _ = self.client(account(), page("same"))
        result = client.edit("Test", "same", "summary")
        self.assertEqual(result["status"], "noop")
        self.assertEqual([call["method"] for call in session.calls], ["GET", "GET"])

    def test_edit_noop_allows_only_terminal_lf_removal(self):
        for actual, expected in (("粤粵\n正文", "粤粵\n正文\n"),
                                 ("正文\n", "正文\n\n"), ("正文\n", "正文")):
            with self.subTest(actual=actual, expected=expected):
                client, session, _ = self.client(account(), page(actual))
                self.assertEqual(client.edit("Test", expected, "summary")["status"], "noop")
                self.assertEqual([call["method"] for call in session.calls], ["GET", "GET"])

    def test_edit_noop_preserves_other_whitespace_and_body_differences(self):
        expected = "粤粵\n正文\n"
        for actual in ("粤粵\n正文 ", "粤粵\n正文\t", "粤粵\n正文\r\n",
                       "粤粵\n正文\u3000", "粤粵\n正文\u00a0", "粤粵\n正文\u2028",
                       "粤粵正文", "粤粵\n\n正文", "粵粵\n正文"):
            with self.subTest(actual=actual):
                client, session, _ = self.client(account(), page(actual), TOKEN,
                                                 {"edit": {"result": "Success", "pageid": 23, "newrevid": 18}})
                self.assertEqual(client.edit("Test", expected, "summary")["status"], "edited")
                self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 1)

    def test_edit_conflicts_preflight(self):
        cases = [(page(), {"base_revision": 16}), (page(), {"base_timestamp": "old"}),
                 (page(), {"create_only": True}), (page(missing=True), {})]
        for before, kwargs in cases:
            client, session, _ = self.client(account(), before)
            with self.assertRaises(ConflictError):
                client.edit("Test", "new", "summary", **kwargs)
            self.assertTrue(all(call["method"] == "GET" for call in session.calls))

    def test_edit_guards_revision_and_identity(self):
        client, session, _ = self.client(account(), page(), TOKEN,
                                         {"edit": {"result": "Success", "pageid": 23, "newrevid": 18,
                                                   "newtimestamp": "2026-01-02T00:00:00Z"}})
        result = client.edit("Test", "new", "summary", base_revision=17)
        data = session.calls[-1]["data"]
        self.assertEqual(data["baserevid"], 17)
        self.assertEqual(data["nocreate"], "1")
        self.assertNotIn("createonly", data)
        self.assertEqual(data["assert"], "user")
        self.assertEqual(data["assertuser"], "Editor")
        self.assertEqual(data["token"], "CSRF-SECRET")
        self.assertEqual(result["status"], "edited")
        self.assertEqual(result["revid"], 18)

    def test_create_only_is_explicit(self):
        client, session, _ = self.client(account(), page(missing=True), TOKEN,
                                         {"edit": {"result": "Success", "pageid": 23, "newrevid": 18}})
        self.assertEqual(client.edit("Test", "new", "summary", create_only=True)["status"], "created")
        self.assertEqual(session.calls[-1]["data"]["createonly"], "1")
        self.assertNotIn("nocreate", session.calls[-1]["data"])

    def test_unknown_edit_readback_matches_or_stops(self):
        for content, recovered in (("new", True), ("other editor's text", False)):
            client, session, _ = self.client(account(), page(), TOKEN, requests.Timeout("private"), page(content, 18))
            if recovered:
                self.assertEqual(client.edit("Test", "new", "summary")["status"], "recovered")
            else:
                with self.assertRaises(AmbiguousWriteError):
                    client.edit("Test", "new", "summary")
            self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 1)

    def test_unknown_edit_recovers_after_server_trims_terminal_lf(self):
        client, session, _ = self.client(account(), page(), TOKEN, requests.Timeout(), page("粤粵\n正文", 18))
        result = client.edit("Test", "粤粵\n正文\n\n", "summary")
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["revid"], 18)
        self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 1)

    def test_unknown_edit_does_not_hide_non_lf_differences(self):
        for actual in ("粤粵\n正文 ", "粤粵\n正文\t", "粤粵\n正文\r\n",
                       "粤粵\n正文\u3000", "粤粵\n正文\u00a0", "粤粵\n正文\u2028",
                       "粤粵正文", "粤粵\n\n正文", "粵粵\n正文", None):
            with self.subTest(actual=actual):
                client, session, _ = self.client(account(), page(), TOKEN, requests.Timeout(), page(actual, 18))
                with self.assertRaises(AmbiguousWriteError):
                    client.edit("Test", "粤粵\n正文\n", "summary")
                self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 1)

    def test_incomplete_success_requires_readback(self):
        client, _, _ = self.client(account(), page(), TOKEN, {"edit": {"result": "Success"}}, page("new", 18))
        self.assertEqual(client.edit("Test", "new", "summary")["status"], "recovered")

    def test_server_edit_conflict_is_known_and_not_retried(self):
        client, session, _ = self.client(account(), page(), TOKEN, {"error": {"code": "editconflict"}})
        with self.assertRaises(ConflictError):
            client.edit("Test", "new", "summary")
        self.assertEqual(len(session.calls), 4)


class UploadTests(ClientCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.png"
        self.blob = b"source bytes"
        self.path.write_bytes(self.blob)

    def upload(self, client, **kwargs):
        return client.upload(self.path, "Test.png", "{{Information}}", "Reviewed upload", **kwargs)

    def test_same_hash_reused_and_name_conflict_blocked(self):
        client, session, _ = self.client(account(), file_response(self.blob))
        self.assertEqual(self.upload(client)["status"], "reused")
        self.assertEqual(len(session.calls), 2)
        client, session, _ = self.client(account(), file_response(b"different"))
        with self.assertRaises(ConflictError):
            self.upload(client)
        self.assertEqual(len(session.calls), 2)

    def test_duplicate_hash_reuses_canonical_other_name(self):
        client, session, _ = self.client(account(), file_response(),
                                         {"query": {"allimages": [fileinfo(self.blob, "Existing.png")]}})
        self.assertEqual(self.upload(client)["file"]["title"], "File:Existing.png")
        self.assertEqual(len(session.calls), 3)

    def test_upload_bytes_and_verify_remote_hash(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN,
                                         {"upload": {"result": "Success", "filename": "Test.png"}}, file_response(self.blob))
        self.assertEqual(self.upload(client)["status"], "uploaded")
        call = session.calls[-2]
        self.assertEqual(call["files"]["file"][1], self.blob)
        self.assertNotIn("url", call["data"])
        self.assertNotIn("ignorewarnings", call["data"])
        self.assertEqual(call["data"]["assertuser"], "Editor")

    def test_upload_warning_is_surfaced_without_override(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN,
                                         {"upload": {"result": "Warning", "warnings": {"exists": "Test.png"}, "filekey": "stash-key"}})
        with self.assertRaises(UploadWarning) as caught:
            self.upload(client)
        self.assertEqual(caught.exception.warnings, {"exists": "Test.png"})
        self.assertEqual(caught.exception.filekey, "stash-key")
        self.assertEqual(len(session.calls), 5)
        self.assertNotIn("ignorewarnings", session.calls[-1]["data"])

    def test_unknown_upload_checked_by_hash_without_replay(self):
        for remote, recovered in ((self.blob, True), (b"other", False), (None, False)):
            client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN,
                                             requests.Timeout("private"), file_response(remote))
            if recovered:
                self.assertEqual(self.upload(client)["status"], "recovered")
            else:
                with self.assertRaises(AmbiguousWriteError):
                    self.upload(client)
            self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 1)

    def small_chunks(self, client):
        # Shrink thresholds to exercise the same protocol with tiny test data.
        self.addCleanup(patch.stopall)
        patch.object(client, "CHUNK_THRESHOLD", 8).start()
        patch.object(client, "MIN_CHUNK_SIZE", 4).start()

    def checkpoint(self, offset=4):
        return {"schema": 1, "api_url": WIKISOURCE_API, "filename": "Test.png",
                "sha1": hashlib.sha1(self.blob).hexdigest(), "filesize": len(self.blob),
                "filekey": "stash-key", "offset": offset, "complete": False}

    def stash(self, offset):
        return {"query": {"stashimageinfo": [{"size": offset, "sha1": hashlib.sha1(self.blob[:offset]).hexdigest()}]}}

    def test_chunk_protocol_and_acknowledged_checkpoints(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN,
                                         {"upload": {"result": "Continue", "filekey": "stash-key", "offset": 4}},
                                         {"upload": {"result": "Continue", "filekey": "stash-key", "offset": 8}},
                                         {"upload": {"result": "Success", "filekey": "stash-key", "offset": 12}},
                                         {"upload": {"result": "Success", "filename": "Test.png"}}, file_response(self.blob))
        self.small_chunks(client)
        checkpoints = []
        self.assertEqual(self.upload(client, chunk_size=4, on_checkpoint=checkpoints.append)["status"], "uploaded")
        writes = [call for call in session.calls if call["method"] == "POST"]
        self.assertEqual([call["data"]["offset"] for call in writes[:-1]], [0, 4, 8])
        self.assertEqual(b"".join(call["files"]["chunk"][1] for call in writes[:-1]), self.blob)
        self.assertEqual(writes[-1]["data"]["filekey"], "stash-key")
        self.assertIsNone(writes[-1]["files"])
        self.assertEqual([state["offset"] for state in checkpoints], [0, 4, 8, 12, 12])
        self.assertTrue(checkpoints[-1]["complete"])

    def test_resume_uses_verified_remote_offset_not_checkpoint_offset(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, self.stash(8), TOKEN,
                                         {"upload": {"result": "Success", "filekey": "stash-key", "offset": 12}},
                                         {"upload": {"result": "Success", "filename": "Test.png"}}, file_response(self.blob))
        self.small_chunks(client)
        self.upload(client, chunk_size=4, checkpoint=self.checkpoint(4))
        writes = [call for call in session.calls if call["method"] == "POST"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0]["data"]["offset"], 8)
        self.assertEqual(writes[0]["files"]["chunk"][1], self.blob[8:])

    def test_resume_rejects_different_stash_bytes_before_write(self):
        bad_stash = {"query": {"stashimageinfo": [{"size": 8, "sha1": "0" * 40}]}}
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, bad_stash)
        self.small_chunks(client)
        with self.assertRaises(ConflictError):
            self.upload(client, chunk_size=4, checkpoint=self.checkpoint())
        self.assertTrue(all(call["method"] == "GET" for call in session.calls))

    def test_fully_stashed_resume_only_commits(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, self.stash(12), TOKEN,
                                         {"upload": {"result": "Success", "filename": "Test.png"}}, file_response(self.blob))
        self.small_chunks(client)
        self.upload(client, chunk_size=4, checkpoint=self.checkpoint())
        writes = [call for call in session.calls if call["method"] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["data"]["filekey"], "stash-key")
        self.assertIsNone(writes[0]["files"])

    def test_unknown_chunk_returns_verified_checkpoint_and_stops(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN,
                                         {"upload": {"result": "Continue", "filekey": "stash-key", "offset": 4}},
                                         requests.Timeout("unknown chunk"), self.stash(8))
        self.small_chunks(client)
        with self.assertRaises(AmbiguousWriteError) as caught:
            self.upload(client, chunk_size=4)
        self.assertEqual(caught.exception.checkpoint["offset"], 8)
        self.assertFalse(caught.exception.checkpoint["complete"])
        self.assertEqual(sum(call["method"] == "POST" for call in session.calls), 2)

    def test_unknown_first_chunk_cannot_be_blindly_resumed(self):
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES, TOKEN, requests.Timeout())
        self.small_chunks(client)
        with self.assertRaises(AmbiguousWriteError) as caught:
            self.upload(client, chunk_size=4)
        self.assertIsNone(caught.exception.checkpoint["filekey"])
        checkpoint = caught.exception.checkpoint
        client, session, _ = self.client(account(), file_response(), EMPTY_HASHES)
        self.small_chunks(client)
        with self.assertRaises(ConflictError):
            self.upload(client, chunk_size=4, checkpoint=checkpoint)
        self.assertTrue(all(call["method"] == "GET" for call in session.calls))

if __name__ == "__main__":
    unittest.main()
