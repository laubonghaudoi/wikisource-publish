"""CLI integrity regressions. All remote clients and authentication are mocked."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wikisource_publish as workflow


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wikisource-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "package"
        self.work.mkdir()
        self.md = self.root / "book.md"
        self.md.write_text("# 測試書\n\n淸字[疑]原文。\n", encoding="utf-8")
        self.pdf = self.root / "book.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nlocal test fixture\n%%EOF\n")
        self.png = self.root / "scan-001.png"
        Image.new("RGB", (7, 5), "red").save(self.png)
        self.metadata = {
            "title": "測試書", "author": "測試作者", "year": 1900,
            "source": "https://example.invalid/fixture",
            "license_wikisource": "{{PD|1800|1900}}",
            "license_commons": "{{PD-scan|PD-US-expired}}",
        }
        self.metadata_path = self.root / "metadata.json"
        self.metadata_path.write_text(json.dumps(self.metadata, ensure_ascii=False))
        self.missing = {"title": "測試書", "missing": True, "content": "", "revid": None,
                        "timestamp": None, "pageid": None, "url": "https://example.invalid/book"}

    def make_job(self, *, image=False, local_pdf=False):
        if image:
            self.md.write_text("# 測試書\n\n淸字[疑]原文。\n\n![圖](scan-001.png)\n")
        pdf = workflow.file_record(self.pdf)
        self.pdf_info = {"name": "Existing source.pdf", "sha1": pdf["sha1"],
                         "mime": "application/pdf", "description_url": "https://example.invalid/pdf"}
        if local_pdf:
            pdf.update(input=str(self.pdf), filename=self.pdf_info["name"], status="reused",
                       remote=self.pdf_info)
        else:
            pdf = {"input": "File:Existing source.pdf", "filename": self.pdf_info["name"],
                   "sha1": pdf["sha1"], "status": "reused", "remote": self.pdf_info}
        images = []
        if image:
            record = workflow.file_record(self.png)
            record.update(target="scan-001.png", alt="圖", width=7, height=5,
                          requested_filename="Existing image.png", commons_filename="Existing image.png",
                          scan_page=1, status="reused")
            images.append(record)
        job = {"schema_version": 1, "status": "prepared", "title": "測試書",
               "markdown": workflow.file_record(self.md), "metadata": dict(self.metadata),
               "pdf": pdf, "images": images, "base_page": dict(self.missing), "categories": []}
        workflow.render_job(job, self.work)
        return job

    def fake_clients(self, job, content=None):
        commons, wiki = Mock(), Mock()
        if content is None:
            wiki.page.return_value = dict(self.missing)
        else:
            wiki.page.return_value = {**self.missing, "missing": False, "content": content,
                                      "revid": 12, "pageid": 1, "timestamp": "2026-01-01T00:00:00Z"}
        body_html = (self.work / "source-body.html").read_text()
        wiki.parse.return_value = {"html": body_html, "warnings": [], "images": []}
        wiki.request.return_value = {"query": {"general": {"maxarticlesize": 2_000_000}}}
        wiki.edit.return_value = {"title": job["title"], "revid": 12,
                                  "url": "https://example.invalid/book"}
        assets = {self.pdf_info["name"]: self.pdf_info}
        for image in job["images"]:
            assets[image["commons_filename"]] = {"name": image["commons_filename"],
                "sha1": image["sha1"], "width": image["width"], "height": image["height"],
                "url": "https://example.invalid/image"}
        commons.file_info.side_effect = assets.get
        commons.find_files_by_sha1.return_value = []
        return commons, wiki

    def test_terminal_newline_normalization_does_not_hide_body_changes(self):
        self.assertTrue(workflow.publication_equal("淸字。\n", "淸字。"))
        self.assertFalse(workflow.publication_equal("淸字。\n", "清字。"))
        self.assertFalse(workflow.publication_equal("淸字。 \n", "淸字。"))

    def test_header_preserves_issuing_body_for_official_documents(self):
        job = self.make_job()
        job['metadata']['lawmaker'] = '編印秘書處'
        header = workflow.make_header(job)
        self.assertIn(' | lawmaker = 編印秘書處\n', header)
        self.assertNotIn(' | author = ', header)

    def test_unresolved_review_package_cannot_authenticate_or_publish(self):
        job = self.make_job()
        job['metadata']['publication_blockers'] = ['Copyright basis needs verification']
        workflow.save_job(self.work, job)
        with patch.object(workflow, 'clients') as factory:
            with self.assertRaisesRegex(workflow.PublicationError, 'for review only'):
                workflow.publish(argparse.Namespace(workdir=str(self.work), credentials=None, update_existing=False))
        factory.assert_not_called()

    def test_reviewed_scan_page_overrides_filename_guess(self):
        job = self.make_job(image=True)
        metadata = dict(self.metadata, image_scan_pages={'scan-001.png': 27})
        self.metadata_path.write_text(json.dumps(metadata))
        commons, wiki = self.fake_clients(job)
        args = argparse.Namespace(markdown=str(self.md), pdf='File:Existing source.pdf',
            metadata=str(self.metadata_path), title=None, workdir=str(self.work), offline_validation=True)
        with patch.object(workflow, 'clients', return_value=(commons, wiki)), patch.object(workflow, 'emit'):
            workflow.prepare(args)
        prepared = json.loads((self.work / 'job.json').read_text())
        self.assertEqual(prepared['images'][0]['scan_page'], 27)
        self.assertIn('PDF掃描第27頁', workflow.description(prepared, prepared['images'][0]))

    def test_server_changed_list_number_is_rejected(self):
        self.md.write_text('# 測試書\n\n3. 第三項\n4. 第四項\n')
        job = self.make_job()
        commons, wiki = self.fake_clients(job)
        source = (self.work / 'source-body.html').read_text()
        self.assertIn('<ol start="3">', source)
        wiki.parse.return_value['html'] = source.replace('<ol start="3">', '<ol start="2">')
        with self.assertRaises(workflow.PublicationError):
            workflow.validate_body(job, self.work, wiki)
        report = json.loads((self.work / 'conversion-validation.json').read_text())
        self.assertTrue(report['text_equal'])
        self.assertTrue(report['structure_equal'])
        self.assertFalse(report['list_signatures_equal'])

    def test_reprepare_published_job_refreshes_changed_media(self):
        job = self.make_job(image=True, local_pdf=True)
        old_image = job["images"][0]["sha256"]
        old_pdf = job["pdf"]["sha256"]
        job["status"] = "published"
        workflow.save_job(self.work, job)
        Image.new("RGB", (7, 5), "blue").save(self.png)
        self.pdf.write_bytes(self.pdf.read_bytes() + b"new fixture version\n")
        commons, wiki = self.fake_clients(job)
        args = argparse.Namespace(markdown=str(self.md), pdf=str(self.pdf), metadata=str(self.metadata_path),
                                  title=None, workdir=str(self.work), offline_validation=True)
        with patch.object(workflow, "clients", return_value=(commons, wiki)), patch.object(workflow, "emit"):
            workflow.prepare(args)
        refreshed = json.loads((self.work / "job.json").read_text())
        self.assertNotEqual(refreshed["images"][0]["sha256"], old_image)
        self.assertNotEqual(refreshed["pdf"]["sha256"], old_pdf)

    def test_verify_rejects_changed_publication_even_if_live_page_matches_it(self):
        job = self.make_job()
        artifact = self.work / "publication.wikitext"
        changed = artifact.read_text().replace("淸字", "錯字")
        artifact.write_text(changed)
        commons, wiki = self.fake_clients(job, content=changed)
        with patch.object(workflow, "clients", return_value=(commons, wiki)), patch.object(workflow, "emit"):
            with self.assertRaises(workflow.PublicationError):
                workflow.verify(argparse.Namespace(workdir=str(self.work)))
        persisted = json.loads((self.work / "job.json").read_text())
        self.assertNotEqual(persisted["status"], "published_api_verified")

    def test_noop_publish_rejects_changed_local_image(self):
        job = self.make_job(image=True)
        existing = (self.work / "publication.wikitext").read_text()
        commons, wiki = self.fake_clients(job, content=existing)
        Image.new("RGB", (7, 5), "blue").save(self.png)
        with patch.object(workflow, "clients", return_value=(commons, wiki)), patch.object(workflow, "emit"):
            with self.assertRaises(workflow.PublicationError):
                workflow.publish(argparse.Namespace(workdir=str(self.work), credentials=None, update_existing=False))
        commons.upload.assert_not_called()
        wiki.edit.assert_not_called()

    def test_noop_publish_never_authenticates_uploads_or_edits(self):
        job = self.make_job(image=True)
        existing = (self.work / "publication.wikitext").read_text()
        commons, wiki = self.fake_clients(job, content=existing)
        with patch.object(workflow, "clients", return_value=(commons, wiki)) as factory, \
             patch.object(workflow, "credentials", side_effect=AssertionError("Credentials must remain unread")), \
             patch.object(workflow, "emit"):
            workflow.publish(argparse.Namespace(workdir=str(self.work),
                credentials=self.root / "must-not-be-read.json", update_existing=False))
        self.assertGreaterEqual(factory.call_count, 1)
        for invocation in factory.call_args_list:
            self.assertFalse(invocation.args and invocation.args[0], "Only unauthenticated clients are needed")
            self.assertFalse(invocation.kwargs.get("path"))
        commons.login.assert_not_called()
        wiki.login.assert_not_called()
        commons.upload.assert_not_called()
        wiki.edit.assert_not_called()
        persisted = json.loads((self.work / "job.json").read_text())
        self.assertEqual(persisted["status"], "published_api_verified")
        self.assertEqual(persisted["verified_revision"], 12)

    def test_verify_rejects_source_comparison_corruption_before_api_access(self):
        self.make_job()
        artifact = self.work / "source-body.html"
        artifact.write_text(artifact.read_text().replace("淸字", "錯字"))
        with patch.object(workflow, "clients") as factory:
            with self.assertRaises(workflow.PublicationError):
                workflow.verify(argparse.Namespace(workdir=str(self.work)))
        factory.assert_not_called()

    def test_verify_rejects_body_corruption_before_api_access(self):
        self.make_job()
        artifact = self.work / "body.wikitext"
        artifact.write_text(artifact.read_text().replace("淸字", "錯字"))
        with patch.object(workflow, "clients") as factory:
            with self.assertRaises(workflow.PublicationError):
                workflow.verify(argparse.Namespace(workdir=str(self.work)))
        factory.assert_not_called()

    def test_verify_rejects_table_signature_corruption_before_api_access(self):
        self.make_job()
        (self.work / "expected-tables.json").write_text(
            json.dumps([{"caption": "不存在的表", "rows": []}], ensure_ascii=False))
        with patch.object(workflow, "clients") as factory:
            with self.assertRaises(workflow.PublicationError):
                workflow.verify(argparse.Namespace(workdir=str(self.work)))
        factory.assert_not_called()

    def test_reused_local_pdf_needs_no_new_upload_source(self):
        job = self.make_job(local_pdf=True)
        del job["metadata"]["source"]
        workflow.save_job(self.work, job)
        commons, wiki = self.fake_clients(job)
        with patch.object(workflow, "clients", return_value=(commons, wiki)), \
             patch.object(workflow, "verify"), patch.object(workflow, "emit"):
            workflow.publish(argparse.Namespace(workdir=str(self.work), credentials=None, update_existing=False))
        commons.upload.assert_not_called()
        wiki.edit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
