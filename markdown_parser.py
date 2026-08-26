"""
Obsidian Markdown → Telegram HTML converter.

Obsidian uses standard CommonMark plus:
  - [[wikilinks]]  and  [[wikilink|alias]]
  - ![[embedded files]]
  - #tags
  - ==highlights==
  - ~~strikethrough~~
  - Frontmatter (YAML between --- delimiters)
  - Callouts  > [!NOTE] ...

Telegram HTML supports:
  <b>, <i>, <u>, <s>, <code>, <pre>, <a href="...">, <tg-spoiler>
  (NO nested block elements inside <a>)
"""
import posixpath
import re
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedNote:
    title: str
    frontmatter: dict
    body_raw: str          # raw markdown without frontmatter
    wikilinks: list[str]   # all [[targets]] found in this note


@dataclass
class ConversionContext:
    """
    Passed into convert_to_telegram_html so wikilinks can be resolved
    to actual Telegram message links.

    note_link_map: { normalized note name/path → Telegram message URL }
    """
    note_link_map: dict[str, str] = field(default_factory=dict)
    source_note_path: str = ""  # vault-relative path of the note being rendered
    chat_username: str = ""   # e.g. "mychat" → t.me/mychat/THREAD/MSG


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter stripping
# ─────────────────────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def strip_frontmatter(text: str) -> tuple[str, dict]:
    """Return (body_without_frontmatter, frontmatter_dict)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, {}
    raw_yaml = m.group(1)
    body = text[m.end():]
    fm: dict = {}
    for line in raw_yaml.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return body, fm


# ─────────────────────────────────────────────────────────────────────────────
# Note parsing
# ─────────────────────────────────────────────────────────────────────────────

# [[note]], [[folder/note]], [[note#heading]] and [[note|caption]].
#
# The prior pattern had an empty alternative in capture group 2.  Together
# with its lazy target, ``[[Meeting]]`` became target ``M`` + caption
# ``eeting``.  The target therefore could not be resolved.
WIKILINK_RE = re.compile(
    r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]"
)


def parse_note(path_stem: str, raw_content: str) -> ParsedNote:
    """Parse raw Obsidian note content into a ParsedNote."""
    body, fm = strip_frontmatter(raw_content)

    # Determine title: frontmatter "title" > first H1 > filename
    title = fm.get("title") or _first_h1(body) or path_stem

    wikilinks = [m.group(1).strip() for m in WIKILINK_RE.finditer(body)]

    return ParsedNote(
        title=title,
        frontmatter=fm,
        body_raw=body,
        wikilinks=wikilinks,
    )


def _first_h1(text: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Telegram HTML conversion
# ─────────────────────────────────────────────────────────────────────────────

MAX_PART = 4000   # safe Telegram HTML message limit


def convert_to_telegram_html(
    note: ParsedNote,
    ctx: ConversionContext,
) -> list[str]:
    """
    Convert a ParsedNote to a list of Telegram HTML message strings.
    Returns multiple strings if the content exceeds MAX_PART characters.
    """
    html = _markdown_to_html(note.body_raw, ctx)
    parts = _split_html(html)
    return parts


def _markdown_to_html(md: str, ctx: ConversionContext) -> str:
    lines = md.split("\n")
    out: list[str] = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Fenced code blocks ─────────────────────────────────────────
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = line.strip()[3:].strip()
                code_lines = []
            else:
                in_code_block = False
                code_content = _escape_html("\n".join(code_lines))
                if code_lang:
                    out.append(
                        f'<pre><code class="language-{_escape_html(code_lang)}">'
                        f"{code_content}</code></pre>"
                    )
                else:
                    out.append(f"<pre>{code_content}</pre>")
            i += 1
            continue

        if in_code_block:
            code_lines.append(line)
            i += 1
            continue

        # ── Callouts  > [!NOTE] / [!WARNING] etc. ─────────────────────
        if line.startswith("> [!"):
            callout_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                callout_lines.append(lines[i][1:].lstrip())
                i += 1
            callout_text = " ".join(callout_lines)
            # strip the [!TYPE] header
            callout_text = re.sub(r"^\[!\w+\]\+?\s*", "", callout_text)
            out.append(
                f"<blockquote>{_inline(callout_text, ctx)}</blockquote>"
            )
            continue

        # ── Blockquotes ────────────────────────────────────────────────
        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i][1:].lstrip())
                i += 1
            quote_html = _inline(" ".join(quote_lines), ctx)
            out.append(f"<blockquote>{quote_html}</blockquote>")
            continue

        # ── Horizontal rule ────────────────────────────────────────────
        if re.match(r"^(---+|===+|\*\*\*+)\s*$", line):
            out.append("─────────────────────")
            i += 1
            continue

        # ── Headings ───────────────────────────────────────────────────
        h_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if h_match:
            level = len(h_match.group(1))
            content = _inline(h_match.group(2), ctx)
            if level == 1:
                out.append(f"<b>━━ {content} ━━</b>")
            elif level == 2:
                out.append(f"<b>▸ {content}</b>")
            else:
                out.append(f"<b>{content}</b>")
            i += 1
            continue

        # ── Task list ──────────────────────────────────────────────────
        # This must precede ordinary unordered lists: a task is also a line
        # starting with "-" and would otherwise never reach this branch.
        task_match = re.match(r"^(\s*)[-*+]\s+\[( |x|X)\]\s+(.+)$", line)
        if task_match:
            done = task_match.group(2).lower() == "x"
            content = task_match.group(3)
            check = "✅" if done else "☐"
            out.append(f"{check} {_inline(content, ctx)}")
            i += 1
            continue

        # ── Unordered list ─────────────────────────────────────────────
        if re.match(r"^(\s*)[-*+]\s+", line):
            depth = len(re.match(r"^(\s*)", line).group(1)) // 2
            content = re.sub(r"^\s*[-*+]\s+", "", line)
            bullet = "  " * depth + ("•" if depth == 0 else "◦")
            out.append(f"{bullet} {_inline(content, ctx)}")
            i += 1
            continue

        # ── Ordered list ───────────────────────────────────────────────
        ol_match = re.match(r"^(\s*)\d+[.)]\s+(.+)$", line)
        if ol_match:
            depth = len(ol_match.group(1)) // 2
            content = ol_match.group(2)
            # find the number
            num_match = re.match(r"^\s*(\d+)", line)
            num = num_match.group(1) if num_match else "1"
            prefix = "  " * depth + f"{num}."
            out.append(f"{prefix} {_inline(content, ctx)}")
            i += 1
            continue

        # ── Tables (basic) ─────────────────────────────────────────────
        if "|" in line and line.strip().startswith("|"):
            table_lines = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            out.append(_table_to_html(table_lines, ctx))
            continue

        # ── Empty line ─────────────────────────────────────────────────
        if not line.strip():
            out.append("")
            i += 1
            continue

        # ── Paragraph ─────────────────────────────────────────────────
        out.append(_inline(line, ctx))
        i += 1

    return "\n".join(out).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Inline formatting
# ─────────────────────────────────────────────────────────────────────────────

def _inline(text: str, ctx: ConversionContext) -> str:
    """Apply inline Markdown → HTML transformations."""
    # Embedded files  ![[...]]  — show as 📎 note
    text = re.sub(
        r"!\[\[([^\]]+)\]\]",
        lambda m: f"📎 <i>{_escape_html(m.group(1))}</i>",
        text,
    )

    # Wikilinks  [[target|alias]]  or  [[target]]
    def replace_wikilink(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = m.group(2).strip() if m.group(2) else target
        link = resolve_wikilink(target, ctx)
        if link:
            return f'<a href="{link}">{_escape_html(alias)}</a>'
        # A link can legitimately point outside the uploaded vault.  Leave it
        # readable, but do not make it look like an italic formatting span.
        return _escape_html(alias)

    text = WIKILINK_RE.sub(replace_wikilink, text)

    # Standard markdown links  [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{_escape_html(m.group(2))}">{_escape_html(m.group(1))}</a>',
        text,
    )

    # Auto-links  <url>
    text = re.sub(
        r"<(https?://[^>]+)>",
        lambda m: f'<a href="{_escape_html(m.group(1))}">{_escape_html(m.group(1))}</a>',
        text,
    )

    # Bold  **text**  or  __text__
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", text)
    text = re.sub(r"__(.+?)__", lambda m: f"<b>{m.group(1)}</b>", text)

    # Italic  *text*  or  _text_  (single)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", lambda m: f"<i>{m.group(1)}</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", lambda m: f"<i>{m.group(1)}</i>", text)

    # Bold-italic  ***text***
    text = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: f"<b><i>{m.group(1)}</i></b>", text)

    # Strikethrough  ~~text~~
    text = re.sub(r"~~(.+?)~~", lambda m: f"<s>{m.group(1)}</s>", text)

    # Highlight  ==text==  → spoiler (closest Telegram equivalent)
    text = re.sub(r"==(.+?)==", lambda m: f"<tg-spoiler>{m.group(1)}</tg-spoiler>", text)

    # Inline code  `code`
    text = re.sub(r"`([^`]+)`", lambda m: f"<code>{_escape_html(m.group(1))}</code>", text)

    # Tags  #tag  (not anchors)
    text = re.sub(r"(?<!\w)#([A-Za-z0-9_/]+)", lambda m: f"<i>#{m.group(1)}</i>", text)

    return text


def resolve_wikilink(target: str, ctx: ConversionContext) -> str | None:
    """Resolve a title, vault-relative path, or relative Obsidian path."""
    cleaned = target.strip().replace("\\", "/")
    if not cleaned:
        return None

    candidates = [cleaned]
    if cleaned.lower().endswith(".md"):
        candidates.append(cleaned[:-3])

    if ctx.source_note_path and cleaned.startswith(("./", "../")):
        source_dir = posixpath.dirname(ctx.source_note_path)
        relative = posixpath.normpath(posixpath.join(source_dir, cleaned))
        candidates.append(relative)
        if relative.lower().endswith(".md"):
            candidates.append(relative[:-3])

    for candidate in candidates:
        link = ctx.note_link_map.get(candidate.casefold())
        if link:
            return link
    return None


def _table_to_html(lines: list[str], ctx: ConversionContext) -> str:
    """Render a Markdown table as monospace-ish HTML."""
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return ""

    # Filter out separator rows (---)
    rows = [r for r in rows if not all(re.match(r"^:?-+:?$", c) for c in r)]

    out = ["<pre>"]
    for idx, row in enumerate(rows):
        cell_str = " │ ".join(row)
        out.append(_escape_html(cell_str))
        if idx == 0 and len(rows) > 1:
            out.append("─" * len(cell_str))
    out.append("</pre>")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# HTML escaping
# ─────────────────────────────────────────────────────────────────────────────

def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Message splitting
# ─────────────────────────────────────────────────────────────────────────────

def _split_html(html: str, limit: int = MAX_PART) -> list[str]:
    """
    Split HTML into chunks ≤ limit characters, splitting only at newlines
    to avoid breaking HTML tags mid-way.
    """
    if len(html) <= limit:
        return [html] if html.strip() else []

    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in html.split("\n"):
        line_len = len(line) + 1  # +1 for the newline
        if current_len + line_len > limit and current:
            parts.append("\n".join(current))
            current = []
            current_len = 0
        # If a single line is longer than limit, hard-split it
        if line_len > limit:
            while line:
                chunk = line[:limit]
                parts.append(chunk)
                line = line[limit:]
            continue
        current.append(line)
        current_len += line_len

    if current:
        parts.append("\n".join(current))

    return [p for p in parts if p.strip()]
