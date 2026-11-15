"""Structural preservation tests; never write source documents or call APIs."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "md_wikitext.py"
spec = importlib.util.spec_from_file_location("md_wikitext", SCRIPT)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class ConversionTests(unittest.TestCase):
    def test_frontmatter_and_only_leading_h1_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.md"
            path.write_text("---\n題名: 書名\n作者: 某人\n---\n\n# 書名\n\n首段。\n\n## 第一章\n\n# 原有另題\n", encoding="utf-8")
            metadata, body = m.load_markdown(path)
        self.assertEqual(metadata["題名"], "書名")
        result = m.convert(body, {})
        self.assertNotIn("書名", result["wikitext"])
        self.assertIn("== 第一章 ==", result["wikitext"])
        self.assertIn("= 原有另題 =", result["wikitext"])

    def test_consecutive_h1_only_one_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.md"
            path.write_text("# 書名\n\n# 正文原題\n\n原文。\n", encoding="utf-8")
            metadata, body = m.load_markdown(path)
        self.assertEqual(metadata["title"], "書名")
        self.assertIn("= 正文原題 =", m.convert(body, {})["wikitext"])
        self.assertEqual(m.canonical_text(m.source_html(body)), "正文原題原文。")

    def test_inline_whitespace_and_ideographic_spaces(self):
        result = m.convert('<div><em>甲</em> <em>乙</em></div>\n\n　原刊空格。　\n\n`a  b`\n\n`　碼 `\n', {})
        self.assertIn("<em>甲</em> <em>乙</em>", result["wikitext"])
        self.assertIn("　原刊空格。　", result["wikitext"])
        self.assertIn("<code>a  b</code>", result["wikitext"])
        self.assertIn("<code>　碼 </code>", result["wikitext"])

    def test_html_table_merges_empty_cells_and_breaks(self):
        body = '''<table><thead><tr><th>甲</th><th>乙</th><th>丙</th></tr></thead>
<tbody><tr><td rowspan="2">一<br>二<br><br>三</td><td colspan="2"></td></tr>
<tr><td>管 | 理</td><td>[疑] {{原文}}</td></tr></tbody></table>'''
        result = m.convert(body, {})
        table = result["expected_tables"][0]
        self.assertEqual(table["rows"][1][0], {"tag": "td", "text": "一\n二\n\n三", "rowspan": 2, "colspan": 1})
        self.assertEqual(table["rows"][1][1]["text"], "")
        self.assertEqual(table["rows"][1][1]["colspan"], 2)
        self.assertIn('| rowspan="2" | 一<br />二<br /><br />三', result["wikitext"])
        self.assertIn('| colspan="2" | \n', result["wikitext"])
        self.assertNotIn("<table", result["wikitext"])
        self.assertIn("&#124;", result["wikitext"])
        self.assertIn("&#91;疑&#93;", result["wikitext"])

    def test_markdown_table_escaped_pipe_alignment_and_blank(self):
        body = "|甲|乙|\n|:---|---:|\n|甲\\|乙| |\n|一<br>二|[疑]|\n"
        result = m.convert(body, {})
        self.assertEqual(result["statistics"]["table_cells"], 6)
        self.assertEqual(result["expected_tables"][0]["rows"][1][0]["text"], "甲|乙")
        self.assertEqual(result["expected_tables"][0]["rows"][1][1]["text"], "")
        self.assertIn('style="text-align:left"', result["wikitext"])

    def test_literal_wiki_syntax_survives_as_text(self):
        body = "正文 [[不是連結]] {{不是模板}} | ''原字'' __NOTOC__ ~~~~。\n"
        wiki = m.convert(body, {})["wikitext"]
        self.assertNotIn("{{不是模板}}", wiki)
        self.assertNotIn("[[不是連結]]", wiki)
        self.assertEqual(m.canonical_text(m.source_html(body)), m.canonical_text(wiki))

    def test_nfc_sensitive_spelling_is_preserved_using_entities(self):
        body = "互相表裏。粤粵淸清决決吿告。I\u0301。\n"
        wiki = m.convert(body, {})["wikitext"]
        self.assertIn("表&#63975;", wiki)
        self.assertIn("I&#769;", wiki)
        self.assertIn("粤粵淸清决決吿告", wiki)
        self.assertEqual(m.canonical_text(m.source_html(body)), m.canonical_text(wiki))
        self.assertEqual(m.unicodedata.normalize("NFC", wiki), wiki)

    def test_mixed_html_lists_and_inline_maps_preserve_order(self):
        body = "前文<br><br>![圖一](一.png)<br>圖說<br><br>後文。\n\n<div class=\"source-map\">\n\n![圖二](二.png)\n\n附記。\n\n- 一 條目\n- 二 條目<br>續文\n\n</div>\n\n另段。\n"
        result = m.convert(body, {"一.png": "Book scan-001.png", "二.png": "Book scan-002.png"})
        self.assertEqual([x["target"] for x in result["images"]], ["一.png", "二.png"])
        wiki = result["wikitext"]
        self.assertIn("前文<br /><br />[[File:Book scan-001.png|frameless|800px|alt=圖一]]<br />圖說", wiki)
        self.assertIn('<div class="source-map">', wiki)
        self.assertIn("* 一 條目\n* 二 條目<br />續文", wiki)
        self.assertIn("</div>\n\n另段。", wiki)
        self.assertNotIn("|圖一]]", wiki)

    def test_image_targets_paths_reference_and_html(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / "一 圖.png").write_bytes(b"fixture")
            body = '![甲](<一 圖.png>)\n\n![乙][ref]\n\n[ref]: 一%20圖.png\n\n<img src="一 圖.png" alt="丙">'
            images = m.collect_images(body, p / "book.md")
            self.assertEqual([i["alt"] for i in images], ["甲", "乙", "丙"])
            self.assertTrue(all(i["path"] == str(p / "一 圖.png") for i in images))
            self.assertEqual(images[0]["target"], "一 圖.png")

    def test_image_title_is_preserved_as_safe_frameless_tooltip(self):
        body = '![替代文字](fig.png "圖說工具提示 | {{原文}} [疑]")\n\n![另一圖](fig.png "thumb")\n'
        result = m.convert(body, {"fig.png": "Example.png"})
        self.assertIn("|frameless|800px|alt=替代文字|<span>圖說工具提示 &#124; &#123;&#123;原文&#125;&#125; &#91;疑&#93;</span>]]", result["wikitext"])
        self.assertIn("|alt=另一圖|<span>thumb</span>]]", result["wikitext"])
        self.assertEqual(result["images"][0]["title"], "圖說工具提示 | {{原文}} [疑]")
        self.assertNotIn("|thumb|", result["wikitext"])

    def test_numeric_html_image_width_and_title_do_not_modify_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fig.png"
            original = b"unchanged image fixture"
            path.write_bytes(original)
            body = '<img src="fig.png" alt="原圖" width="100" title="100px">'
            images = m.collect_images(body, Path(directory) / "book.md")
            result = m.convert(body, {"fig.png": "Example.png"})
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(images[0]["alt"], "原圖")
        self.assertEqual(result["wikitext"], "[[File:Example.png|frameless|100px|alt=原圖|<span>100px</span>]]\n")
        self.assertEqual(result["images"][0]["width"], 100)

    def test_image_width_rejects_non_pixel_or_zero_values(self):
        for width in ["100%", "100px", "-10", "0", "1.5", ""]:
            with self.subTest(width=width), self.assertRaises(m.ConversionError):
                m.convert(f'<img src="fig.png" width="{width}">', {"fig.png": "Example.png"})

    def test_plain_image_keeps_existing_output(self):
        self.assertEqual(m.convert("![原圖](fig.png)", {"fig.png": "Example.png"})["wikitext"], "[[File:Example.png|frameless|800px|alt=原圖]]\n")

    def test_pdf_page_mapping_preserves_filename_page_and_alt(self):
        result = m.convert("![原頁](page-0015.png)", {"page-0015.png": {"filename": "Book.pdf", "page": 15}})
        self.assertEqual(result['wikitext'], '[[File:Book.pdf|page=15|frameless|800px|alt=原頁]]\n')
        self.assertEqual(result['images'], [{'target': 'page-0015.png', 'filename': 'Book.pdf', 'page': 15, 'alt': '原頁'}])

    def test_pdf_page_mapping_works_with_width_title_and_plain_images(self):
        body = '<img src="page.png" width="400" alt="頁" title="thumb">\n\n![圖](fig.png)\n'
        result = m.convert(body, {'page.png': {'filename': 'File:Book.PDF', 'page': 1}, 'fig.png': 'Figure.png'})
        self.assertEqual(result['wikitext'], '[[File:Book.PDF|page=1|frameless|400px|alt=頁|<span>thumb</span>]]\n\n[[File:Figure.png|frameless|800px|alt=圖]]\n')
        self.assertEqual(result['images'][0]['page'], 1)
        self.assertNotIn('page', result['images'][1])

    def test_pdf_page_mapping_rejects_invalid_values_and_injections(self):
        mappings = [{'filename': 'Book.pdf', 'page': page} for page in (0, -1, True, False, None, '15', 1.5, '1|thumb')]
        mappings += [{'filename': name, 'page': 1} for name in ('Book.png', 'Book.pdf|thumb', 'Book.pdf\n', '', '.pdf', None, 'Book.pdf#page=1')]
        mappings += [{'filename': 'Book.pdf'}, {'page': 1}, {'filename': 'Book.pdf', 'page': 1, 'extra': 'thumb'}]
        for mapping in mappings:
            with self.subTest(mapping=mapping), self.assertRaises(m.ConversionError):
                m.convert('![頁](page.png)', {'page.png': mapping})

    def test_signatures_ignore_indentation_but_not_br_or_empty_cell(self):
        a = '<table><tr><th>欄</th><td>甲<br>乙</td><td></td></tr></table>'
        b = '<div class="mw-parser-output"><table class="wikitable"><tbody><tr>\n<th>欄\n</th>\n<td>甲<br />乙\n</td><td>\n</td></tr></tbody></table></div>'
        self.assertEqual(m.table_signatures(a), m.table_signatures(b))
        self.assertNotEqual(m.table_signatures(a), m.table_signatures(a.replace("<br>", " ")))

    def test_canonical_text_excludes_only_generated_ui(self):
        html = '<style>.x{}</style><div id="toc" class="toc">目錄</div><h2>章<span class="mw-editsection">編輯</span></h2><p>甲 [疑] 粤。<img src="x" alt="不顯示"></p>'
        self.assertEqual(m.canonical_text(html), "章甲[疑]粤。")
        self.assertNotEqual(m.canonical_text(html), "章甲[疑]粵。")

    def test_nested_list_and_loose_paragraphs(self):
        body = "- 首項\n  - 子項\n- 末項\n\n  末項續段。\n"
        wiki = m.convert(body, {})["wikitext"]
        self.assertIn("* 首項\n** 子項\n* 末項", wiki)
        self.assertIn("<br /><br />", wiki)

    def test_ordered_list_non_one_start_preserves_numbering(self):
        for start in (0, 2, 7):
            with self.subTest(start=start):
                body = f"{start}. 甲\n{start + 1}. 乙\n"
                wiki = m.convert(body, {})["wikitext"]
                self.assertIn(f'<ol start="{start}">', wiki)
                self.assertEqual(m.canonical_text(m.source_html(body)), m.canonical_text(wiki))
                self.assertEqual([n.tag for n in m._walk(m._parse(wiki))], ["root", "ol", "li", "li"])

    def test_nested_single_number_lists_keep_start_and_parent_items(self):
        body = "- 第四條\n  - 1. 甲\n  - 2. 乙\n  - 3. 丙\n- 第五條\n"
        source = m._parse(m.source_html(body))
        rendered = m._parse(m.convert(body, {})["wikitext"], strict=True)
        def list_structure(root):
            return [(n.tag, n.attrs.get("start", "1")) for n in m._walk(root) if n.tag in {"ul", "ol", "li"}]
        self.assertEqual(list_structure(source), list_structure(rendered))
        self.assertEqual([n.attrs.get("start", "1") for n in m._walk(rendered) if n.tag == "ol"], ["1", "2", "3"])

    def test_paragraph_after_nested_list_stays_in_same_item(self):
        body = "- 第六條\n  - 甲、合格條件\n\n  但有左列情事者：\n\n  - 乙、不得參選\n- 第七條\n"
        wiki = m.convert(body, {})["wikitext"]
        source = m._parse(m.source_html(body))
        rendered = m._parse(wiki, strict=True)
        def block_structure(root):
            return [(n.tag, [c.tag for c in n.children if isinstance(c, m.Node)])
                    for n in m._walk(root) if n.tag in {"ul", "ol", "li", "p"}]
        self.assertEqual(block_structure(source), block_structure(rendered))
        self.assertEqual(m.canonical_text(m.source_html(body)), m.canonical_text(wiki))
        self.assertIn("<p>但有左列情事者：</p>", wiki)

    def test_simple_lists_keep_native_output(self):
        body = "- 甲\n  - 乙\n- 丙\n\n1. 丁\n2. 戊\n"
        self.assertEqual(m.convert(body, {})["wikitext"], "* 甲\n** 乙\n* 丙\n\n# 丁\n# 戊\n")

    def test_list_signatures_check_numbering_and_item_ownership(self):
        source = '<ul><li><p>第六條</p><ol start="2"><li>甲</li></ol><p>但有例外</p><ul><li>乙</li></ul></li></ul>'
        same = '<ul><li>第六條<ol start="2"><li>甲</li></ol>但有例外<ul><li>乙</li></ul></li></ul>'
        changed_start = same.replace('start="2"', 'start="1"')
        detached_paragraph = same.replace('但有例外', '</li><li>但有例外')
        self.assertEqual(m.list_signatures(source), m.list_signatures(same))
        self.assertNotEqual(m.list_signatures(source), m.list_signatures(changed_start))
        self.assertNotEqual(m.list_signatures(source), m.list_signatures(detached_paragraph))
        self.assertEqual(m.list_signatures(source)[0]['items'][0][1]['start'], 2)

    def test_missing_map_and_unsupported_html_fail(self):
        for body in ["![圖](missing.png)", "<script>bad()</script>", '<table><tr><td style="color:red">甲</td></tr></table>', "<details>隱藏文字</details>"]:
            with self.subTest(body=body), self.assertRaises(m.ConversionError):
                m.convert(body, {})


if __name__ == "__main__":
    unittest.main()
