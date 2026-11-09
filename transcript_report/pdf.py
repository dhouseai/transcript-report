"""Turning a Markdown report into a PDF.

The Markdown is converted to HTML here (covering what the reports use: headings, bold/italic,
inline code, links, tables, bullet/numbered/checkbox lists, quotes, code blocks and rules),
then Microsoft Edge (or Chrome) prints it to PDF in headless mode. Edge comes with Windows
and is signed, so Smart App Control allows it, and no Python packages are needed.
"""

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .helpers import NO_WINDOW

if sys.platform == "win32":
    BROWSERS = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        *([Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/Application/chrome.exe"]
          if os.environ.get("LOCALAPPDATA") else []),
    ]
elif sys.platform == "darwin":
    BROWSERS = [Path("/Applications") / app for app in (
        "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "Google Chrome.app/Contents/MacOS/Google Chrome",
        "Chromium.app/Contents/MacOS/Chromium",
    )]
else:
    BROWSERS = []   # Linux: found on PATH by find_browser()
BROWSER_COMMANDS = ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable", "chrome",
                    "chromium", "chromium-browser")

CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: "Segoe UI", Arial, "Segoe UI Emoji", sans-serif; font-size: 10.5pt;
       line-height: 1.45; color: #1a1a1a; }
h1 { font-size: 20pt; margin: 0 0 8pt; border-bottom: 2px solid #333; padding-bottom: 4pt; }
h2 { font-size: 14pt; margin: 18pt 0 6pt; border-bottom: 1px solid #ccc; padding-bottom: 2pt;
     break-after: avoid; }
h3 { font-size: 11.5pt; margin: 12pt 0 4pt; break-after: avoid; }
h4, h5, h6 { font-size: 10.5pt; margin: 10pt 0 4pt; }
p { margin: 4pt 0 6pt; }
ul, ol { margin: 4pt 0 6pt; padding-left: 18pt; }
li { margin: 2pt 0; }
li.task { list-style: none; margin-left: -14pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9.5pt; }
th, td { border: 1px solid #bbb; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #eee; }
tr { break-inside: avoid; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f2f2f2; padding: 0 2pt; }
pre { background: #f2f2f2; padding: 6pt; white-space: pre-wrap; font-size: 9pt; }
pre code { background: none; padding: 0; }
blockquote { margin: 6pt 0; padding: 2pt 10pt; border-left: 3px solid #bbb; color: #444; }
hr { border: 0; border-top: 1px solid #ccc; margin: 12pt 0; }
a { color: #1a4fa0; }
"""

_CODE_RE = re.compile(r"`([^`]+)`")
# The URL may hold one level of brackets, as Wikipedia's do: .../Placebo_(disambiguation)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")
PLACEHOLDER = "\uE000"   # private-use character marking a stashed snippet: \uE000<n>\uE000
_STASH_RE = re.compile(PLACEHOLDER + r"(\d+)" + PLACEHOLDER)
SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:")


def inline(text: str) -> str:
    """Escape HTML, then apply inline Markdown: `code`, **bold**, *italic*, [links](url).

    Code spans and link tags are swapped for placeholders first, so emphasis markers inside
    them (e.g. https://x.com/_a_/) are left alone and nothing is escaped twice. Only http(s)
    and mailto links become links; anything else stays as plain text."""
    stash: list[str] = []

    def keep(snippet: str) -> str:
        stash.append(snippet)
        return f"{PLACEHOLDER}{len(stash) - 1}{PLACEHOLDER}"

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not url.lower().startswith(SAFE_LINK_SCHEMES):
            return m.group(0)
        return keep(f'<a href="{html.escape(url)}">') + label + keep("</a>")

    text = text.replace(PLACEHOLDER, "")
    text = _CODE_RE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK_RE.sub(link, text)
    s = html.escape(text, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    # __bold__ only around more than one word, so Python names like __init__ stay as they are.
    s = re.sub(r"(?<![\w_])__(?=\S)([^_]*?\s[^_]*?)(?<=\S)__(?![\w_])", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])", r"<em>\1</em>", s)
    return _STASH_RE.sub(lambda m: stash[int(m.group(1))], s)


def table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def is_table_divider(line: str) -> bool:
    """A |---|:---:| row. It must contain a |, so a plain --- rule isn't taken for one."""
    return "|" in line and bool(
        re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", line))


LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")   # closing #s need a space before them


def next_nonblank(lines: list[str], i: int) -> int:
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def render_list(lines: list[str], i: int, body: list[str]) -> int:
    """Append the list starting at lines[i] (nesting by indent) to body; return the next line.
    Blank lines between items don't end the list, so "1. a", "", "2. b" stays one list."""
    stack: list[tuple[int, str]] = []   # (indent, tag)
    while i < len(lines):
        m = LIST_RE.match(lines[i])
        if not m:
            if not lines[i].strip():
                j = next_nonblank(lines, i)
                if j < len(lines) and LIST_RE.match(lines[j]):
                    i = j
                    continue
                break
            if stack and lines[i][:1] in (" ", "\t"):    # continuation of the last item
                body[-1] = body[-1][:-len("</li>")] + " " + inline(lines[i].strip()) + "</li>"
                i += 1
                continue
            break
        indent, marker, item = len(m.group(1).replace("\t", "    ")), m.group(2), m.group(3)
        tag = "ol" if marker[0].isdigit() else "ul"
        while stack and indent < stack[-1][0]:
            body.append(f"</{stack.pop()[1]}>")
        if stack and indent == stack[-1][0] and tag != stack[-1][1]:
            body.append(f"</{stack.pop()[1]}>")          # bullets <-> numbers at the same level
        if not stack or indent > stack[-1][0]:
            stack.append((indent, tag))
            start = int(marker[:-1]) if tag == "ol" else 1
            body.append(f'<ol start="{start}">' if start != 1 else f"<{tag}>")
        task = re.match(r"^\[([ xX])\]\s+(.*)$", item)
        if task:
            box = "☑" if task.group(1).lower() == "x" else "☐"
            body.append(f'<li class="task">{box} {inline(task.group(2))}</li>')
        else:
            body.append(f"<li>{inline(item)}</li>")
        i += 1
    while stack:
        body.append(f"</{stack.pop()[1]}>")
    return i


def render_table(lines: list[str], i: int, body: list[str]) -> int:
    """Append the table whose header is lines[i] to body; return the next line."""
    head = table_cells(lines[i])
    rows = []
    i += 2
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        rows.append(table_cells(lines[i]))
        i += 1
    t = ["<table><thead><tr>"] + [f"<th>{inline(c)}</th>" for c in head] + ["</tr></thead><tbody>"]
    for r in rows:
        r = r + [""] * (len(head) - len(r))
        t += ["<tr>"] + [f"<td>{inline(c)}</td>" for c in r] + ["</tr>"]
    body.append("".join(t) + "</tbody></table>")
    return i


def markdown_to_html(md: str, title: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    body: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            body.append("<p>" + "<br>".join(inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_para()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            body.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        m = HEADING_RE.match(stripped)
        if m:
            flush_para()
            n = len(m.group(1))
            body.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para()
            body.append("<hr>")
            i += 1
            continue

        if "|" in stripped and i + 1 < len(lines) and is_table_divider(lines[i + 1]):
            flush_para()
            i = render_table(lines, i, body)
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            body.append("<blockquote>" + "<br>".join(inline(q) for q in quote) + "</blockquote>")
            continue

        if LIST_RE.match(line):
            flush_para()
            i = render_list(lines, i, body)
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>\n" + "\n".join(body) + "\n</body></html>")


def find_browser() -> Path | None:
    for p in BROWSERS:
        if p.is_file():
            return p
    for name in BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def write_pdf(markdown: str, pdf_path: Path, title: str) -> None:
    """Render the Markdown to pdf_path. Raises RuntimeError if it can't."""
    browser = find_browser()
    if not browser:
        raise RuntimeError("Microsoft Edge or Google Chrome is needed to make PDFs and wasn't found.")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    # Edge's helper processes can still hold files in the profile folder for a moment after
    # it exits; that mustn't turn a finished PDF into an error.
    with tempfile.TemporaryDirectory(prefix="report_pdf_", ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "report.html"
        page.write_text(markdown_to_html(markdown, title), encoding="utf-8")
        out = Path(tmp) / "report.pdf"
        cmd = [str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={Path(tmp) / 'profile'}",
               "--no-pdf-header-footer", "--print-to-pdf-no-header",
               f"--print-to-pdf={out}", page.as_uri()]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{browser.name} took too long to make the PDF.") from None
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError(f"{browser.name} didn't produce a PDF. {(r.stderr or r.stdout).strip()[-500:]}")
        shutil.move(out, pdf_path)
