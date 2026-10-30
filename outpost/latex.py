"""Turn Polygon statement LaTeX into HTML.

Polygon statements are written in a small LaTeX subset (``olymp.sty``). Math is
left for KaTeX in the browser — inline ``$…$``/``$$$…$$$`` becomes ``\\(…\\)``
and display ``$$…$$``/``$$$$$$…$$$$$$`` becomes ``\\[…\\]`` — and the text
around it is converted here: paragraphs, emphasis, lists, simple tables,
images, links and the usual typographic ligatures. Unknown commands keep their
argument text, so nothing a setter wrote silently disappears.
"""

from __future__ import annotations

import html
import re

_MATH = re.compile(
    r"\$\$\$\$\$\$(.+?)\$\$\$\$\$\$"   # $$$$$$ display $$$$$$  (Codeforces style)
    r"|\$\$\$(.+?)\$\$\$"              # $$$ inline $$$
    r"|\$\$(.+?)\$\$"                  # $$ display $$
    r"|\\\[(.+?)\\\]"                  # \[ display \]
    r"|\\\((.+?)\\\)"                  # \( inline \)
    r"|(?<!\\)\$(.+?)(?<!\\)\$",       # $ inline $
    re.S,
)

_SIMPLE = {  # \command{arg} -> tag
    "textbf": "b", "bf": "b", "textit": "i", "it": "i", "emph": "em", "textsl": "i",
    "texttt": "code", "t": "code", "tt": "code", "underline": "u", "textsc": "span",
    "sout": "s", "textrm": "span", "textsf": "span", "mbox": "span",
}

_SYMBOLS = [
    ("---", "\u2014"), ("--", "\u2013"), ("<<", "\u00ab"), (">>", "\u00bb"),
    ("``", "\u201c"), ("''", "\u201d"), ("~", "\u00a0"),
]

_ESCAPES = {r"\%": "%", r"\$": "$", r"\&": "&amp;", r"\#": "#", r"\_": "_", r"\{": "{",
            r"\}": "}", r"\ldots": "\u2026", r"\dots": "\u2026", r"\textbackslash": "\\",
            r"\LaTeX": "LaTeX", r"\TeX": "TeX", r"\,": "\u2009", r"\ ": " ", r"\-": ""}


def to_html(tex: str | None, image_url=None) -> str:
    """Convert a statement section. ``image_url(name)`` maps image file names to URLs."""
    if not tex or not tex.strip():
        return ""
    maths: list[str] = []

    def stash(m: re.Match) -> str:
        groups = m.groups()
        body = next(g for g in groups if g is not None)
        display = groups[0] is not None or groups[2] is not None or groups[3] is not None
        open_, close = (r"\[", r"\]") if display else (r"\(", r"\)")
        maths.append(open_ + html.escape(body.strip(), quote=False) + close)
        return f"\x00{len(maths) - 1}\x00"

    text = re.sub(r"(?<!\\)%[^\n]*", "", tex)          # comments
    text = _MATH.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _environments(text, image_url)
    text = _commands(text, image_url)
    for a, b in _ESCAPES.items():
        text = text.replace(html.escape(a, quote=False), b)
    for a, b in _SYMBOLS:
        text = text.replace(html.escape(a, quote=False), b)
    text = text.replace("\\\\", "<br>")
    text = _paragraphs(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: maths[int(m.group(1))], text)


def _arg(text: str, start: int) -> tuple[str, int] | None:
    """Read a balanced ``{…}`` group starting at ``text[start]``."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return None


def _commands(text: str, image_url) -> str:
    out, i = [], 0
    cmd = re.compile(r"\\([A-Za-z]+)\*?\s*(\[[^\]]*\])?")
    while True:
        m = cmd.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        name, opt = m.group(1), m.group(2)
        a = _arg(text, m.end())
        if name in _SIMPLE and a:
            tag = _SIMPLE[name]
            out.append(f"<{tag}>{_commands(a[0], image_url)}</{tag}>")
            i = a[1]
        elif name == "includegraphics" and a:
            src = image_url(a[0].strip()) if image_url else a[0].strip()
            out.append(f'<img class="stmt-img" src="{html.escape(src)}" alt="">')
            i = a[1]
        elif name == "url" and a:
            out.append(f'<a href="{a[0]}">{a[0]}</a>')
            i = a[1]
        elif name == "href" and a:
            b = _arg(text, a[1])
            label = _commands(b[0], image_url) if b else a[0]
            out.append(f'<a href="{a[0]}">{label}</a>')
            i = b[1] if b else a[1]
        elif name in ("section", "subsection", "subsubsection", "paragraph") and a:
            out.append(f"\n\n<h4>{_commands(a[0], image_url)}</h4>\n\n")
            i = a[1]
        elif name in ("bf", "it", "tt", "small", "large", "Large", "normalsize", "centering",
                      "noindent", "medskip", "bigskip", "smallskip", "newline", "hfill",
                      "vspace", "hspace", "par", "linebreak"):
            if name in ("newline", "linebreak"):
                out.append("<br>")
            if name in ("vspace", "hspace") and a:
                i = a[1]
            elif name == "par":
                out.append("\n\n")
                i = m.end()
            else:
                i = m.end()
        elif "\\" + name in _ESCAPES:
            out.append(m.group(0))   # handled after
            i = m.end()
        elif a:                       # unknown \cmd{x}: keep x
            out.append(_commands(a[0], image_url))
            i = a[1]
        else:
            out.append(m.group(0) if opt is None else "\\" + name)
            i = m.end()
    return "".join(out)


_ENV = re.compile(r"\\begin\{(\w+\*?)\}(\{[^}]*\})?(.*?)\\end\{\1\}", re.S)


def _environments(text: str, image_url) -> str:
    def repl(m: re.Match) -> str:
        name, body = m.group(1), _environments(m.group(3), image_url)
        if name in ("itemize", "enumerate"):
            tag = "ul" if name == "itemize" else "ol"
            items = [s.strip() for s in re.split(r"\\item\b", body)[1:]]
            lis = "".join(f"<li>{_paragraphs(it, inline=True)}</li>" for it in items)
            return f"\n\n<{tag}>{lis}</{tag}>\n\n"
        if name in ("tabular", "tabularx", "array"):
            return "\n\n" + _table(body) + "\n\n"
        if name == "center":
            return f'\n\n<div class="center">{body}</div>\n\n'
        if name in ("verbatim", "lstlisting"):
            return f"\n\n<pre>{body.strip()}</pre>\n\n"
        if name in ("figure", "figure*", "wrapfigure"):
            return f"\n\n{body}\n\n"
        return body

    prev = None
    while prev != text:
        prev, text = text, _ENV.sub(repl, text)
    return text


def _table(body: str) -> str:
    body = re.sub(r"\\(hline|cline\{[^}]*\})", "", body)
    rows = [r for r in re.split(r"\\\\", body) if r.strip()]
    trs = []
    for n, row in enumerate(rows):
        cells = [c.strip() for c in row.split("&amp;")]
        tag = "th" if n == 0 else "td"
        trs.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
    return '<table class="stmt-table">' + "".join(trs) + "</table>"


_BLOCK = re.compile(r"^\s*<(ul|ol|table|div|pre|h4)\b")


def _paragraphs(text: str, inline: bool = False) -> str:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if inline:
        return "<br>".join(parts)
    return "\n".join(p if _BLOCK.match(p) else f"<p>{p}</p>" for p in parts)
