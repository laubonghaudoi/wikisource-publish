"""Focused tests for publisher submodules, Collection header, notes/cover safeguards, DI hooks and CLI."""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mediawiki_client import AmbiguousWriteError  # noqa: E402
from publisher import (  # noqa: E402
    PublicationError,
    RuntimeHooks,
    description,
    digest,
    extract_remote_categories,
    make_header,
    validate_metadata_structure,
    validate_pdf_embeds,
)
from publisher.metadata import missing_required_fields  # noqa: E402
from publisher.workflow import prepare as direct_prepare  # noqa: E402
import wikisource_publish as cli  # noqa: E402


class PublisherModulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wikisource-pub-modules-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.md = self.root / "book.md"
        self.md.write_text("# 廣東扣械潮\n\n## 序\n\n正文。\n", encoding="utf-8")
        self.pdf = self.root / "book.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF\n")

    def test_collection_header_with_override_collector_and_author_fallback(self):
        meta = {
            "title": "廣東扣械潮",
            "override_collector": "香港華字日報社",
            "year": 1924,
            "source": "https://example.invalid/source",
            "license_wikisource": "{{PD-anon-expired}}",
            "license_commons": "{{PD-scan|PD-US-expired}}",
            "notes": "底本見[[:File:廣東扣械潮.pdf]]。",
        }
        validate_metadata_structure("廣東扣械潮", meta)
        self.assertEqual(missing_required_fields(meta), [])

        job = {"title": "廣東扣械潮", "metadata": meta, "pdf": {"filename": "廣東扣械潮.pdf"}}
        header = make_header(job)
        self.assertTrue(header.startswith("{{Collection header\n"))
        self.assertIn(" | override_collector = 香港華字日報社\n", header)
        self.assertIn(" | notes = 底本見[[:File:廣東扣械潮.pdf]]。\n", header)
        self.assertNotIn(" | author = ", header)
        self.assertIn("|author=香港華字日報社\n", description(job))

    def test_override_collector_cannot_combine_with_lawmaker(self):
        meta = {
            "override_collector": "香港華字日報社",
            "lawmaker": "大本營",
            "license_commons": "{{PD-scan}}",
        }
        with self.assertRaisesRegex(PublicationError, "Cannot combine"):
            validate_metadata_structure("廣東扣械潮", meta)

    def test_notes_rejects_unprefixed_file_embeds_and_allows_colon_links(self):
        for bad in ("見[[File:Book.pdf]]。", "見[[檔案:Book.pdf]]", "見[[Image:Cover.jpg|thumb]]"):
            with self.subTest(bad=bad), self.assertRaisesRegex(PublicationError, r"\[\[:File:"):
                validate_metadata_structure("測試書", {"author": "甲", "notes": bad})

        validate_metadata_structure("測試書", {"author": "甲", "notes": "見[[:File:Book.pdf]]。"})

    def test_cover_image_and_omit_pdf_thumb_adjust_header_and_embed_validation(self):
        base_meta = {"author": "甲", "year": 1924}
        cover_job = {
            "title": "廣東扣械潮",
            "metadata": {**base_meta, "cover_image": "File:廣東扣械潮 彩色封面.jpg"},
            "pdf": {"filename": "廣東扣械潮.pdf"},
            "image_order": [{"page": 15}],
        }
        cover_header = make_header(cover_job)
        self.assertIn("[[File:廣東扣械潮 彩色封面.jpg|360px|center|廣東扣械潮封面]]", cover_header)
        self.assertNotIn("原文掃描]]", cover_header)
        html_with_body_only = (
            '<img src="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/'
            '%E5%BB%A3%E6%9D%B1%E6%89%A3%E6%A2%B0%E6%BD%AE.pdf/page15-800px.jpg">'
        )
        self.assertTrue(validate_pdf_embeds(html_with_body_only, cover_job))

        omitted_job = {**cover_job, "metadata": {**base_meta, "omit_pdf_thumb": True}}
        self.assertTrue(make_header(omitted_job).endswith("}}\n\n"))
        self.assertTrue(validate_pdf_embeds(html_with_body_only, omitted_job))

    def test_extract_remote_categories_handles_multilingual_prefixes(self):
        text = "正文\n[[Category:香港華字日報]]\n[[分類:廣東文獻]]\n[[分类:政治]]\n"
        self.assertEqual(
            extract_remote_categories(text),
            ["[[Category:香港華字日報]]", "[[分類:廣東文獻]]", "[[分类:政治]]"],
        )

    def test_direct_workflow_prepare_uses_injected_runtime_hooks(self):
        meta_path = self.root / "meta.json"
        meta_path.write_text(json.dumps({
            "title": "廣東扣械潮",
            "override_collector": "香港華字日報社",
            "year": 1924,
            "license_wikisource": "{{PD-anon-expired}}",
            "license_commons": "{{PD-scan|PD-US-expired}}",
            "cover_image": "廣東扣械潮 彩色封面.jpg",
        }, ensure_ascii=False), encoding="utf-8")

        commons, wiki = Mock(), Mock()
        commons.find_files_by_sha1.return_value = [{"name": "廣東扣械潮.pdf", "sha1": digest(self.pdf, "sha1")}]
        wiki.page.return_value = {"title": "廣東扣械潮", "missing": True, "content": "", "revid": None, "url": "https://zh.wikisource.org/wiki/test"}
        events = []
        work = self.root / "work"
        args = type("Args", (), {
            "markdown": str(self.md),
            "pdf": str(self.pdf),
            "metadata": str(meta_path),
            "title": None,
            "workdir": str(work),
            "offline_validation": True,
        })()
        prep = direct_prepare(
            args,
            hooks=RuntimeHooks(
                clients_fn=lambda path=None: (commons, wiki),
                emit_fn=lambda event, **data: events.append((event, data)),
            ),
        )
        self.assertEqual(prep["layout_mode"], "single_page")
        self.assertEqual(events[0][0], "prepared")
        pub = (work / "publication.wikitext").read_text(encoding="utf-8")
        self.assertIn("{{Collection header\n", pub)
        self.assertIn("[[File:廣東扣械潮 彩色封面.jpg|360px|center|廣東扣械潮封面]]", pub)

    def test_cli_help_and_interruption_handling(self):
        from contextlib import redirect_stdout
        from unittest.mock import patch

        work = self.root / "work"
        work.mkdir()
        buf = io.StringIO()
        with patch.object(
            cli._workflow,
            "publish",
            side_effect=AmbiguousWriteError("timeout", checkpoint={"offset": 100}),
        ), patch.object(sys, "argv", ["wikisource_publish.py", "publish", "--workdir", str(work)]), redirect_stdout(buf):
            rc = cli.main()
        self.assertEqual(rc, 1)
        interruption = json.loads((work / "write-interruption.json").read_text(encoding="utf-8"))
        self.assertEqual(interruption["error_type"], "AmbiguousWriteError")
        self.assertEqual(interruption["checkpoint"], {"offset": 100})
        event = json.loads(buf.getvalue().strip())
        self.assertEqual(event["event"], "stopped")
        self.assertEqual(event["details_file"], "write-interruption.json")

        res = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/wikisource_publish.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("prepare", res.stdout)
        self.assertIn("publish", res.stdout)
        self.assertIn("verify", res.stdout)


if __name__ == "__main__":
    unittest.main()
