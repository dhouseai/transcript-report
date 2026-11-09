import unittest

from transcript_report.pdf import inline, is_table_divider, markdown_to_html, table_cells


def body(md: str) -> str:
    html = markdown_to_html(md, "t")
    return html[html.index("<body>") + len("<body>"):html.index("</body>")].strip()


class Inline(unittest.TestCase):
    def test_escapes_html(self):
        self.assertEqual(inline("<script>&"), "&lt;script&gt;&amp;")

    def test_link_escaped_once(self):
        self.assertEqual(inline("[q](https://x.com/?a=1&b=2)"),
                         '<a href="https://x.com/?a=1&amp;b=2">q</a>')

    def test_no_emphasis_inside_urls(self):
        self.assertEqual(inline("[v](https://x.com/_foo_/bar)"),
                         '<a href="https://x.com/_foo_/bar">v</a>')

    def test_only_safe_link_schemes(self):
        self.assertEqual(inline("[x](javascript:alert(1))"), "[x](javascript:alert(1))")

    def test_code_bold_italic(self):
        self.assertEqual(inline("a `<b>*x*</b>` c *i* **b**"),
                         "a <code>&lt;b&gt;*x*&lt;/b&gt;</code> c <em>i</em> <strong>b</strong>")

    def test_python_names_left_alone(self):
        self.assertEqual(inline("see __init__ and my_var_name"), "see __init__ and my_var_name")
        self.assertEqual(inline("__very big__"), "<strong>very big</strong>")


class Blocks(unittest.TestCase):
    def test_numbered_list_with_blank_lines_is_one_list(self):
        self.assertEqual(body("1. one\n\n2. two\n\n3. three").count("<ol"), 1)

    def test_list_start_number(self):
        self.assertIn('<ol start="3">', body("intro\n\n3. three\n4. four"))

    def test_nested_and_continued_lists(self):
        self.assertEqual(body("- a\n  - b\n- c").count("<ul>"), 2)
        self.assertIn("<li>item more</li>", body("- item\n\tmore"))

    def test_task_list(self):
        self.assertIn("☑ done", body("- [x] done"))

    def test_headings(self):
        self.assertEqual(body("# C#"), "<h1>C#</h1>")
        self.assertEqual(body("## Title ##"), "<h2>Title</h2>")

    def test_table(self):
        out = body("| h | g |\n|---|:--:|\n| a\\|b | c |\n| short |")
        self.assertIn("<th>h</th><th>g</th>", out)
        self.assertIn("<td>a|b</td><td>c</td>", out)
        self.assertIn("<td>short</td><td></td>", out)

    def test_rule_is_not_a_table_divider(self):
        self.assertFalse(is_table_divider("---"))
        self.assertNotIn("<table", body("a | b\n---\nx"))

    def test_code_block_is_escaped(self):
        self.assertEqual(body("```\n<b>&\n```"), "<pre><code>&lt;b&gt;&amp;</code></pre>")

    def test_unclosed_code_block(self):
        self.assertIn("<pre><code>x</code></pre>", body("```\nx"))

    def test_title_escaped(self):
        self.assertIn("<title>&lt;x&gt;</title>", markdown_to_html("", "<x>"))

    def test_table_cells(self):
        for row, cells in (("| a | b |", ["a", "b"]),
                           ("a | b", ["a", "b"]),                        # no outer pipes
                           ("| a \\| b | c |", ["a | b", "c"]),          # escaped pipe inside a cell
                           ("| a | b \\|", ["a", "b |"])):               # escaped pipe at the end
            with self.subTest(row=row):
                self.assertEqual(table_cells(row), cells)


if __name__ == "__main__":
    unittest.main()
