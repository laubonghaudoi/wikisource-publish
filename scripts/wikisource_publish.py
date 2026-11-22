#!/usr/bin/env python3
"""Prepare, publish and verify one Markdown book on Chinese Wikisource."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Ensure sibling modules under scripts/ resolve cleanly regardless of cwd.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from mediawiki_client import AmbiguousWriteError, MediaWikiError, UploadWarning  # noqa: E402
from publisher.media import (  # noqa: E402,F401
    collect_and_prepare_images,
    commons_name,
    ensure_remote_asset,
    image_mapping,
    read_pdf_page_map,
    resolve_pdf,
)
from publisher.metadata import (  # noqa: E402,F401
    default_edit_summary,
    description,
    format_header_link,
    layout_mode_for,
    make_header,
    make_subpage_toc,
    normalize_categories,
    validate_metadata_structure,
    validate_subpages,
    validate_wiki_page_target,
)
from publisher.models import (  # noqa: E402,F401
    COMMONS_API,
    CREDENTIALS,
    PublicationError,
    SOURCE_API,
    checked_local,
    clients as _default_clients,
    credentials,
    digest,
    emit,
    file_record,
    filename,
    first_difference,
    json_write,
    now,
    publication_equal,
    save_job,
    text_parameter,
)
from publisher.validation import (  # noqa: E402,F401
    StructureReader,
    document_structure,
    duplicate_h2_headings,
    layout_diagnostics,
    validate_body,
    validate_pdf_embeds,
)
from publisher import workflow as _workflow  # noqa: E402
from publisher.workflow import (  # noqa: E402,F401
    RuntimeHooks,
    assemble_publication,
    checked_package,
    render_job,
)


def clients(path=None):
    """Build Commons/Wikisource clients respecting module-level credential hooks."""
    return _default_clients(path, credentials_fn=credentials)


def _hooks() -> RuntimeHooks:
    return RuntimeHooks(clients_fn=clients, emit_fn=emit, verify_fn=verify)


def prepare(args):
    """Delegate prepare workflow through the module compatibility hooks."""
    return _workflow.prepare(args, hooks=_hooks())


def publish(args):
    """Delegate publish workflow through the module compatibility hooks."""
    return _workflow.publish(args, hooks=_hooks())


def verify(args):
    """Delegate verify workflow through the module compatibility hooks."""
    return _workflow.verify(args, hooks=_hooks())


def main() -> int:
    """Parse CLI arguments and run prepare, publish or verify with safe error JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    prep = subs.add_parser("prepare", help="Read-only remote checks; create a local publication package.")
    prep.add_argument("--markdown", required=True)
    prep.add_argument("--pdf", required=True)
    prep.add_argument("--metadata", help="Agent-prepared JSON of verified metadata and license templates.")
    prep.add_argument(
        "--pdf-page-map",
        help="Reviewed JSON image_pages mapping for unmodified full pages of an existing Commons PDF.",
    )
    prep.add_argument("--title")
    prep.add_argument("--workdir")
    prep.add_argument(
        "--offline-validation",
        action="store_true",
        help="Defer only server parser validation; remote source checks still run.",
    )
    prep.set_defaults(func=prepare)

    pub = subs.add_parser(
        "publish",
        help="Upload missing media and publish the prepared article using authorized credentials.",
    )
    pub.add_argument("--workdir", required=True)
    pub.add_argument("--credentials", type=Path, default=CREDENTIALS)
    pub.add_argument(
        "--update-existing",
        action="store_true",
        help="Use only when the user authorized updating this existing title.",
    )
    pub.set_defaults(func=publish)

    ver = subs.add_parser("verify", help="Re-read the live article and verify content, tables, PDF and images.")
    ver.add_argument("--workdir", required=True)
    ver.set_defaults(func=verify)

    args = parser.parse_args()
    try:
        args.func(args)
    except (UploadWarning, AmbiguousWriteError) as exc:
        detail = {
            "time_utc": now(),
            "error": str(exc),
            "error_type": type(exc).__name__,
            "warnings": getattr(exc, "warnings", {}),
            "checkpoint": getattr(exc, "checkpoint", None),
        }
        if getattr(args, "workdir", None):
            json_write(Path(args.workdir) / "write-interruption.json", detail)
        emit("stopped", error=str(exc), error_type=type(exc).__name__, details_file="write-interruption.json")
        return 1
    except (PublicationError, MediaWikiError, OSError, ValueError) as exc:
        emit("stopped", error=str(exc), error_type=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
