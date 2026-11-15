"""Loss-conscious Markdown/HTML to Wikisource wikitext conversion.

No network access or writes. Image targets are logical Markdown destinations
(before MarkdownIt's URL encoding), including raw HTML img src destinations.
Tables are parsed structurally; their signatures also accept MediaWiki HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
from typing import Iterator
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import image as _image_rule
import yaml


class ConversionError(ValueError):
    """Input cannot be represented without silently losing content."""


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCKS = {"p", "div", "blockquote", "pre", "ul", "ol", "li", "table", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
IGNORED_UI = {"toc", "mw-toc", "mw-editsection", "mw-editsection-like", "mw-empty-elt", "mw-file-backlink"}


class _DOM(HTMLParser):
    def __init__(self, strict: bool = False):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.strict = strict

    def handle_starttag(self, tag, attrs):
        if len({k for k, _ in attrs}) != len(attrs):
            raise ConversionError(f"Duplicate attributes on <{tag}>")
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.strict and self.stack[-1].tag != tag:
            raise ConversionError(f"Unbalanced HTML: expected </{self.stack[-1].tag}>, got </{tag}>")
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        if self.strict:
            raise ConversionError(f"Unmatched HTML closing tag </{tag}>")

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def finish(self):
        self.close()
        if self.strict and len(self.stack) != 1:
            raise ConversionError(f"Unclosed HTML tag <{self.stack[-1].tag}>")
        return self.root


def _parse(html: str, *, strict=False) -> Node:
    parser = _DOM(strict)
    parser.feed(html)
    return parser.finish()


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _walk(child)


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "typographer": False, "linkify": False}).enable("table")

    def image_rule(state, silent):
        start = state.pos
        success = _image_rule(state, silent)
        if success and not silent:
            token = state.tokens[-1]
            label_end = state.md.helpers.parseLinkLabel(state, start + 1, False)
            pos = label_end + 1
            target = unquote(token.attrGet("src") or "")
            if pos < len(state.src) and state.src[pos] == "(":
                pos += 1
                while pos < len(state.src) and state.src[pos] in " \t\n":
                    pos += 1
                dest = state.md.helpers.parseLinkDestination(state.src, pos, len(state.src))
                if dest.ok:
                    target = dest.str
            token.attrSet("data-source-target", target)
        return success

    md.inline.ruler.at("image", image_rule)
    return md


def _protect_spaces(text: str) -> tuple[str, dict[str, str]]:
    # MarkdownIt uses Python strip() in a few block rules. Unlike CommonMark's
    # ASCII whitespace, that also removes printed U+3000/NBSP. Temporary unused
    # private-use characters preserve these in text, code, and HTML alike.
    spaces = sorted({c for c in text if c.isspace() and c not in " \t\r\n\f\v"})
    substitutions = {}
    candidate = 0xE000
    for space in spaces:
        while chr(candidate) in text or chr(candidate) in substitutions:
            candidate += 1
        if candidate > 0xF8FF:
            raise ConversionError("Unable to preserve source Unicode whitespace")
        substitutions[chr(candidate)] = space
        candidate += 1
    for placeholder, space in substitutions.items():
        text = text.replace(space, placeholder)
    return text, substitutions


def _restore_spaces(text: str, substitutions: dict[str, str]) -> str:
    for placeholder, space in substitutions.items():
        text = text.replace(placeholder, space).replace(quote(placeholder), quote(space))
    return text


def _without_title(body: str) -> tuple[str, str | None]:
    protected, substitutions = _protect_spaces(body)
    tokens = _markdown().parse(protected)
    if tokens and tokens[0].type == "heading_open" and tokens[0].tag == "h1":
        first, last = tokens[0].map
        lines = body.splitlines(keepends=True)
        title = _restore_spaces(tokens[1].content, substitutions)
        return "".join(lines[:first] + lines[last:]).lstrip("\r\n"), title
    return body, None


def load_markdown(path) -> tuple[dict, str]:
    """Read UTF-8 and remove YAML; source_html/convert remove the book H1 once."""
    text = Path(path).read_text(encoding="utf-8-sig")
    metadata = {}
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is None:
            raise ConversionError("Unclosed YAML frontmatter")
        metadata = yaml.safe_load("".join(lines[1:end])) or {}
        if not isinstance(metadata, dict):
            raise ConversionError("YAML frontmatter must be a mapping")
        text = "".join(lines[end + 1:])
    _, title = _without_title(text)
    if title and not any(metadata.get(k) for k in ("title", "題名", "書名")):
        metadata["title"] = title
    return metadata, text


def source_html(body: str) -> str:
    """Render the publication body (excluding the leading book H1) for checks."""
    body, _ = _without_title(body)
    protected, substitutions = _protect_spaces(body)
    return _restore_spaces(_markdown().render(protected), substitutions)


def _image_target(node: Node) -> str:
    return node.attrs.get("data-source-target", node.attrs.get("src", ""))


def collect_images(body: str, markdown_path) -> list[dict]:
    """Return every image occurrence in document order; paths must be local files."""
    base = Path(markdown_path).resolve().parent
    result = []
    for node in _walk(_parse(source_html(body), strict=True)):
        if node.tag != "img":
            continue
        target = _image_target(node)
        parsed = urlsplit(target)
        if not target or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ConversionError(f"Image must reference a local file: {target!r}")
        path = (base / unquote(parsed.path)).resolve()
        if not path.is_file():
            raise ConversionError(f"Image file does not exist: {path}")
        result.append({"target": target, "path": str(path), "alt": node.attrs.get("alt", "")})
    return result


def _ignored(node: Node) -> bool:
    return node.tag in {"style", "script", "noscript", "meta", "link"} or bool(set(node.attrs.get("class", "").split()) & IGNORED_UI)


def _visible(node: Node | str, *, cell=False) -> str:
    if isinstance(node, str):
        # HTML layout whitespace is not an explicit line break.
        return re.sub(r"[\t\r\n\f ]+", " ", node)
    if _ignored(node) or node.tag == "img":
        return ""
    if node.tag == "br":
        return "\n"
    value = "".join(_visible(c, cell=cell) for c in node.children)
    if cell and node.tag in {"p", "div", "li", "pre", "blockquote"}:
        return "\n" + value + "\n"
    return value


def _cell_text(node: Node) -> str:
    value = _visible(node, cell=True)
    # Preserve explicit brs and empty lines, but discard indentation around them.
    return "\n".join(re.sub(r"[ \t\r\f]+", " ", line).strip(" \t\r\f") for line in value.split("\n")).strip(" \t\r\f")


def canonical_text(html: str) -> str:
    """Visible body text, ignoring ASCII layout whitespace and generated Wiki UI.

    Images contribute no alt/caption text. Characters, punctuation, doubt marks,
    and ideographic spaces are not normalized; HTML entities are decoded once.
    Compare body-only parses, without the separate Header/license templates.
    """
    return re.sub(r"[ \t\r\n\f\v]+", "", _visible(_parse(html)))


def _span(node: Node, name: str) -> int:
    raw = node.attrs.get(name, "1")
    if not raw.isascii() or not raw.isdigit() or int(raw) < 1:
        raise ConversionError(f"Unsupported {name}={raw!r}")
    return int(raw)


def _table_rows(table: Node) -> list[Node]:
    rows = []
    for child in table.children:
        if isinstance(child, str):
            if child.strip():
                raise ConversionError("Text outside a table cell")
        elif child.tag == "tr":
            rows.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            for row in child.children:
                if isinstance(row, str) and not row.strip():
                    continue
                if not isinstance(row, Node) or row.tag != "tr":
                    raise ConversionError(f"Unexpected table group child: {row!r}")
                rows.append(row)
        elif child.tag != "caption":
            raise ConversionError(f"Unsupported table child <{child.tag}>")
    return rows


def _signature(table: Node) -> dict:
    rows = []
    for row in _table_rows(table):
        cells = []
        for cell in row.children:
            if isinstance(cell, str) and not cell.strip():
                continue
            if not isinstance(cell, Node) or cell.tag not in {"th", "td"}:
                raise ConversionError("Unexpected non-cell inside table row")
            cells.append({"tag": cell.tag, "text": _cell_text(cell), "rowspan": _span(cell, "rowspan"), "colspan": _span(cell, "colspan")})
        rows.append(cells)
    caption = next((_cell_text(c) for c in table.children if isinstance(c, Node) and c.tag == "caption"), "")
    return {"caption": caption, "rows": rows}


def table_signatures(html: str) -> list[dict]:
    """Canonical source/MediaWiki table signatures, retaining brs and empty cells."""
    return [_signature(n) for n in _walk(_parse(html)) if n.tag == "table"]


def list_signatures(html: str) -> list[dict]:
    """Compare list starts, item ownership, nesting and intervening text.

    Paragraph wrappers and ASCII layout whitespace may differ after parsing;
    item text and its position before/between/after child lists may not.
    """
    def signature(node):
        start = node.attrs.get("start", "1")
        if node.tag == "ol" and not re.fullmatch(r"-?[0-9]+", start):
            raise ConversionError(f"Unsupported ordered list start: {start!r}")
        items = []
        for item in node.children:
            if isinstance(item, str) and not item.strip():
                continue
            if not isinstance(item, Node) or item.tag != "li":
                raise ConversionError("Non-item content in list signature")
            segments, pending = [], []

            def flush():
                value = re.sub(r"[ \t\r\n\f\v]+", "", "".join(pending))
                if value:
                    segments.append(value)
                pending.clear()

            def visit(child):
                if isinstance(child, str):
                    pending.append(child)
                elif not _ignored(child) and child.tag != "img":
                    if child.tag in {"ul", "ol"}:
                        flush()
                        segments.append(signature(child))
                    else:
                        for nested in child.children:
                            visit(nested)

            for child in item.children:
                visit(child)
            flush()
            items.append(segments)
        return {"tag": node.tag, "start": int(start) if node.tag == "ol" else None, "items": items}

    def collect(node):
        if _ignored(node):
            return []
        if node.tag in {"ul", "ol"}:
            return [signature(node)]
        return [result for child in node.children if isinstance(child, Node) for result in collect(child)]

    return collect(_parse(html))


_WIKI_SPECIAL = {c: f"&#{ord(c)};" for c in "[]{}|'=*#:;!_~-`"}


def escape_wikitext(text: str) -> str:
    """Escape syntax and NFC-sensitive characters without changing spelling.

    MediaWiki normalizes API text before parsing it, including compatibility
    ideographs such as U+F9E7. Entities are decoded after that normalization.
    Encoding combining marks also preserves deliberately decomposed accents.
    """
    return "".join(
        f"&#{ord(c)};" if unicodedata.normalize("NFC", c) != c or unicodedata.combining(c)
        else _WIKI_SPECIAL.get(c, escape(c, quote=False))
        for c in text
    )


def _attrs(node: Node, allowed=()):
    bad = set(node.attrs) - set(allowed)
    if bad:
        raise ConversionError(f"Unsupported attributes on <{node.tag}>: {', '.join(sorted(bad))}")


def _class_attr(node: Node) -> str:
    value = node.attrs.get("class", "")
    return f' class="{escape(value, quote=True)}"' if value else ""


class _Converter:
    def __init__(self, image_map):
        self.image_map = image_map
        self.images = []

    def image(self, node):
        _attrs(node, {"src", "alt", "data-source-target", "title", "width"})
        target = _image_target(node)
        if target not in self.image_map:
            raise ConversionError(f"No Commons filename mapped for image: {target}")
        mapped = self.image_map[target]
        page = None
        if isinstance(mapped, dict):
            if set(mapped) != {"filename", "page"}:
                raise ConversionError(f"PDF image mapping requires only filename and page: {target!r}")
            filename, page = mapped["filename"], mapped["page"]
            if type(page) is not int or page < 1:
                raise ConversionError(f"PDF page must be a positive integer: {target!r}")
        else:
            filename = mapped
        if not isinstance(filename, str) or not filename.strip() or filename != filename.strip() or any(c in filename for c in "|[]{}<>#\r\n"):
            raise ConversionError(f"Invalid Commons filename for image {target!r}")
        if filename.lower().startswith(("file:", "image:")):
            filename = filename.split(":", 1)[1]
        if page is not None and (not filename.lower().endswith(".pdf") or filename.lower() == ".pdf"):
            raise ConversionError(f"Page mapping must name a Commons PDF file: {target!r}")
        alt = node.attrs.get("alt", "")
        record = {"target": target, "alt": alt, "filename": filename}
        if page is not None:
            record["page"] = page
        width = 800
        if "width" in node.attrs:
            raw_width = node.attrs["width"].strip(" \t\r\n\f")
            if not re.fullmatch(r"[0-9]+", raw_width):
                raise ConversionError(f"Image width must be a positive integer pixel count: {raw_width!r}")
            try:
                width = int(raw_width)
            except ValueError as error:
                raise ConversionError("Image width is outside the supported integer range") from error
            if width < 1:
                raise ConversionError("Image width must be greater than zero")
            record["width"] = width
        tooltip = ""
        if "title" in node.attrs:
            title = node.attrs["title"]
            record["title"] = title
            # Help:Images: a caption on a frameless image is a tooltip, not a
            # visible caption. The span prevents titles such as "thumb", "left"
            # or "100px" from becoming File options; literal syntax is escaped.
            tooltip = "|<span>" + escape_wikitext(title) + "</span>"
        self.images.append(record)
        page_option = f"|page={page}" if page is not None else ""
        return f"[[File:{filename}{page_option}|frameless|{width}px|alt={escape_wikitext(alt)}{tooltip}]]"

    def inline(self, children) -> str:
        result = []
        for node in children:
            if isinstance(node, str):
                result.append(escape_wikitext(re.sub(r"[\t\r\n\f ]+", " ", node)))
                continue
            tag = node.tag
            if tag == "img":
                result.append(self.image(node))
            elif tag == "br":
                _attrs(node)
                result.append("<br />")
            elif tag in {"em", "strong", "b", "i", "u", "s", "sup", "sub", "small", "code"}:
                _attrs(node)
                if tag == "code":
                    if any(not isinstance(c, str) for c in node.children):
                        raise ConversionError("Nested markup in inline code")
                    content = escape_wikitext("".join(node.children))
                else:
                    content = self.inline(node.children)
                result.append(f"<{tag}>" + content + f"</{tag}>")
            elif tag == "a":
                _attrs(node, {"href", "title"})
                if "title" in node.attrs:
                    raise ConversionError("Link tooltips are not supported")
                href = node.attrs.get("href", "")
                label = self.inline(node.children)
                if href.startswith("#"):
                    result.append(f"[[{href}|{label}]]")
                elif urlsplit(href).scheme in {"http", "https"}:
                    href = quote(href, safe=":/?&=+%~@!$'()*,;#-._")
                    result.append(f"[{escape(href, quote=False)} {label}]")
                else:
                    raise ConversionError(f"Unsupported link destination: {href!r}")
            else:
                raise ConversionError(f"Unsupported inline HTML <{tag}>")
        return "".join(result)

    def table(self, table) -> str:
        _attrs(table, {"class"})
        classes = " ".join(dict.fromkeys(["wikitable", *table.attrs.get("class", "").split()]))
        lines = [f'{{| class="{escape(classes, quote=True)}"']
        for group in table.children:
            if isinstance(group, Node) and group.tag in {"thead", "tbody", "tfoot"}:
                _attrs(group)
            if isinstance(group, Node) and group.tag == "caption":
                _attrs(group)
                lines.append("|+ " + self.inline(group.children))
        for row in _table_rows(table):
            _attrs(row)
            lines.append("|-")
            for cell in row.children:
                if isinstance(cell, str) and not cell.strip():
                    continue
                if not isinstance(cell, Node) or cell.tag not in {"th", "td"}:
                    raise ConversionError("Unexpected non-cell inside table row")
                _attrs(cell, {"rowspan", "colspan", "style", "scope"})
                attributes = []
                for key in ("rowspan", "colspan"):
                    if key in cell.attrs:
                        attributes.append(f'{key}="{_span(cell, key)}"')
                if "style" in cell.attrs:
                    style = cell.attrs["style"]
                    if not re.fullmatch(r"text-align:\s*(left|right|center);?", style):
                        raise ConversionError(f"Unsupported table cell style: {style!r}")
                    attributes.append(f'style="{style}"')
                if "scope" in cell.attrs:
                    if cell.attrs["scope"] not in {"row", "col", "rowgroup", "colgroup"}:
                        raise ConversionError("Unsupported table header scope")
                    attributes.append(f'scope="{cell.attrs["scope"]}"')
                marker = "!" if cell.tag == "th" else "|"
                prefix = marker + " " + (" ".join(attributes) + " | " if attributes else "")
                # Cell paragraphs are explicit HTML, never blank wiki lines that
                # could terminate the containing native table.
                content = []
                for child in cell.children:
                    if isinstance(child, Node) and child.tag == "p":
                        _attrs(child)
                        content.append("<p>" + self.inline(child.children) + "</p>")
                    else:
                        content.append(self.inline([child]))
                lines.append(prefix + "".join(content))
        lines.append("|}")
        return "\n".join(lines)

    @staticmethod
    def _list_requires_html(node) -> bool:
        # Wiki * / # syntax cannot express a non-1 start or return to the same
        # parent item after a nested list. Preserve that entire list as HTML.
        for descendant in _walk(node):
            if descendant.tag == "ol" and descendant.attrs.get("start", "1") != "1":
                return True
            if descendant.tag == "li":
                seen_nested = False
                for child in descendant.children:
                    if isinstance(child, Node) and child.tag in {"ul", "ol"}:
                        seen_nested = True
                    elif seen_nested and (isinstance(child, Node) or child.strip(" \t\r\n\f\v")):
                        return True
        return False

    def _html_list(self, node) -> str:
        _attrs(node, {"start"} if node.tag == "ol" else set())
        start = ""
        if "start" in node.attrs:
            value = node.attrs["start"]
            if not re.fullmatch(r"-?[0-9]+", value):
                raise ConversionError(f"Unsupported ordered list start: {value!r}")
            start = f' start="{value}"'
        lines = [f"<{node.tag}{start}>"]
        for item in node.children:
            if isinstance(item, str) and not item.strip():
                continue
            if not isinstance(item, Node) or item.tag != "li":
                raise ConversionError("Non-item content in list")
            _attrs(item)
            content = []
            for child in item.children:
                if isinstance(child, Node) and child.tag in {"ul", "ol"}:
                    content.append(self._html_list(child))
                elif isinstance(child, Node) and child.tag == "p":
                    _attrs(child)
                    content.append("<p>" + self.inline(child.children) + "</p>")
                else:
                    content.append(self.inline([child]))
            lines.append("<li>" + "".join(content) + "</li>")
        lines.append(f"</{node.tag}>")
        return "\n".join(lines)

    def list(self, node, prefix="") -> str:
        if self._list_requires_html(node):
            return self._html_list(node)
        _attrs(node, {"start"} if node.tag == "ol" else set())
        prefix += "*" if node.tag == "ul" else "#"
        lines = []
        for item in node.children:
            if isinstance(item, str) and not item.strip():
                continue
            if not isinstance(item, Node) or item.tag != "li":
                raise ConversionError("Non-item content in list")
            _attrs(item)
            content = []
            emitted = False
            for child in item.children:
                if isinstance(child, Node) and child.tag in {"ul", "ol"}:
                    if content or not emitted:
                        lines.append(prefix + " " + "".join(content).strip(" \t\r\n\f\v"))
                        content = []
                        emitted = True
                    lines.append(self.list(child, prefix))
                elif isinstance(child, Node) and child.tag == "p":
                    _attrs(child)
                    if content:
                        content.append("<br /><br />")
                    content.append(self.inline(child.children))
                elif isinstance(child, str) and not child.strip() and not content:
                    continue
                else:
                    content.append(self.inline([child]))
            if content or not emitted:
                if emitted:
                    raise ConversionError("List item text following a nested list requires explicit handling")
                lines.append(prefix + " " + "".join(content).strip(" \t\r\n\f\v"))
        return "\n".join(lines)

    def blocks(self, children) -> str:
        result = []
        pending = []

        def flush():
            if pending:
                value = self.inline(pending).strip(" \t\r\n\f\v")
                if value:
                    result.append(value)
                pending.clear()

        for node in children:
            if isinstance(node, str):
                if node.strip() or pending:
                    pending.append(node)
                continue
            tag = node.tag
            if tag not in BLOCKS and tag != "hr":
                pending.append(node)
                continue
            flush()
            if tag == "p":
                _attrs(node)
                result.append(self.inline(node.children).strip(" \t\r\n\f\v"))
            elif tag in {f"h{i}" for i in range(1, 7)}:
                _attrs(node)
                marks = "=" * int(tag[1])
                result.append(marks + " " + self.inline(node.children) + " " + marks)
            elif tag == "table":
                result.append(self.table(node))
            elif tag in {"ul", "ol"}:
                result.append(self.list(node))
            elif tag == "div":
                _attrs(node, {"class"})
                result.append("<div" + _class_attr(node) + ">\n\n" + self.blocks(node.children) + "\n\n</div>")
            elif tag == "blockquote":
                _attrs(node)
                result.append("<blockquote>\n\n" + self.blocks(node.children) + "\n\n</blockquote>")
            elif tag == "hr":
                _attrs(node)
                result.append("----")
            elif tag == "pre":
                _attrs(node)
                if len(node.children) != 1 or not isinstance(node.children[0], Node) or node.children[0].tag != "code":
                    raise ConversionError("Preformatted content must contain one code element")
                code = node.children[0]
                _attrs(code, {"class"})
                if code.attrs.get("class", "") and not re.fullmatch(r"language-[A-Za-z0-9_+-]+", code.attrs["class"]):
                    raise ConversionError("Unsupported code block class")
                if any(not isinstance(c, str) for c in code.children):
                    raise ConversionError("Nested markup in code block")
                result.append("<pre>" + escape_wikitext("".join(code.children)) + "</pre>")
            else:
                raise ConversionError(f"Unsupported block HTML <{tag}>")
        flush()
        return "\n\n".join(result)


def convert(body: str, image_map: dict[str, str | dict]) -> dict:
    """Map every image to a Commons name or {filename: PDF name, page: int}."""
    rendered = source_html(body)
    root = _parse(rendered, strict=True)
    expected = table_signatures(rendered)
    converter = _Converter(image_map)
    wikitext = converter.blocks(root.children).rstrip() + "\n"
    nodes = list(_walk(root))
    return {
        "wikitext": wikitext,
        "expected_tables": expected,
        "images": converter.images,
        "statistics": {
            "images": len(converter.images),
            "tables": len(expected),
            "table_rows": sum(len(t["rows"]) for t in expected),
            "table_cells": sum(len(row) for t in expected for row in t["rows"]),
            "paragraphs": sum(n.tag == "p" for n in nodes),
            "list_items": sum(n.tag == "li" for n in nodes),
            "headings": {f"h{i}": sum(n.tag == f"h{i}" for n in nodes) for i in range(1, 7)},
        },
    }
