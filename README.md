# wikisource-publish

An [Agent Skills](https://agentskills.io/) skill for publishing proofread Markdown and its source PDF, images, and structured tables to Chinese Wikisource (`zh.wikisource.org`) and Wikimedia Commons.

Depending on length and internal structure, works are published either as:
- **Single full-text page** (default for short and medium works up to ~300 KB wikitext), or
- **Substantive root page + semantic subpages** (for multi-volume or multi-subject compilations, avoiding empty index shells or Index/Page page-by-page fragmentation while optimizing readability, SEO, and GEO).

The skill preserves original wording, historic variants, punctuation, uncertain `[...]` spans, image geometry, and native wikitables (`rowspan`/`colspan`). It never re-OCRs or modernizes source text.

## Installation

Install the skill into your agent skills directory (for example `~/.gemini/config/skills/` or `~/.codex/skills/`) by symlink or copy:

```bash
git clone https://github.com/laubonghaudoi/wikisource-publish.git
ln -s "$PWD/wikisource-publish" ~/.gemini/config/skills/wikisource-publish
```

Install Python dependencies (`requests`, `markdown-it-py`, `PyYAML`, `Pillow`):

```bash
python3 -m pip install -r wikisource-publish/scripts/requirements.txt
```

Configure a Wikimedia BotPassword in your own terminal (stored with `0600` permissions at `~/.config/wikisource-publish/credentials.json`):

```bash
python3 wikisource-publish/scripts/configure_credentials.py
```

## Architecture

```text
scripts/
├── wikisource_publish.py       # Thin CLI & backwards-compatible facade (prepare / publish / verify)
├── configure_credentials.py    # Interactive 0600 Wikimedia BotPassword setup
├── md_wikitext.py              # Lossless Markdown/HTML DOM -> Wikisource wikitext & table signatures
├── mediawiki_client.py         # Safe MediaWiki Action API client (CSRF, conflict guards, chunked upload)
└── publisher/
    ├── models.py               # Errors, streaming hashes, atomic JSON IO, escaping & credentials
    ├── metadata.py             # Page/subpage validation, {{header}} / {{Collection header}}, TOC & descriptions
    ├── media.py                # Commons PDF lookup, reviewed PDF-page-map & verified image uploads
    ├── validation.py           # Server action=parse parity checks, PDF embed order & layout advisories
    └── workflow.py             # Decoupled prepare / publish / verify pipelines with RuntimeHooks DI
```

## Verification

Run the offline unit test suite:

```bash
python3 -m unittest discover -s tests -v
```

See [SKILL.md](SKILL.md) and [references/metadata-and-api.md](references/metadata-and-api.md) for the full publication workflow, metadata schema, and browser QA checklist.
