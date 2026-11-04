"""Syntax colours for source files.

The whole file is tokenized at once, because a line inside a docstring or block
comment can't be coloured from that line alone. Results are keyed by the byte
offset of each line, which is how the view addresses rows.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Optional

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename, guess_lexer
from pygments.lexers.special import TextLexer
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound

from .ansi import Span

# Pygments runs at a few MB/s and holds the GIL, so bigger files stay plain.
MAX_HIGHLIGHT_BYTES = 2 * 1024 * 1024
TAB_WIDTH = 8
LIGHT_STYLE = "default"
DARK_STYLE = "github-dark"
LEXER_OPTIONS = dict(stripnl=False, ensurenl=False)


@dataclass
class Highlighted:
    language: str
    lines: dict[int, list[Span]] = field(default_factory=dict)


def lexer_for_name(name: str) -> Optional[Lexer]:
    try:
        lexer = get_lexer_for_filename(name, **LEXER_OPTIONS)
    except ClassNotFound:
        return None
    return None if isinstance(lexer, TextLexer) else lexer


def worth_fetching(name: str) -> bool:
    """Named like code, or without a suffix and so possibly a script."""
    return lexer_for_name(name) is not None or not posixpath.splitext(name)[1]


def _lexer_for(name: str, data: bytes) -> Optional[Lexer]:
    lexer = lexer_for_name(name)
    if lexer is None and data.startswith(b"#!"):
        first_line = data.split(b"\n", 1)[0].decode("utf-8", "replace")
        try:
            lexer = guess_lexer(first_line, **LEXER_OPTIONS)
        except ClassNotFound:
            return None
        if isinstance(lexer, TextLexer):
            return None
    return lexer


def _expand(piece: str, column: int) -> str:
    """Expand tabs in a piece that starts at ``column`` of its line."""
    if "\t" not in piece:
        return piece
    lead = column % TAB_WIDTH
    return (" " * lead + piece).expandtabs(TAB_WIDTH)[lead:]


def highlight(name: str, data: bytes, dark: bool, expand_tabs: bool = True) -> Optional[Highlighted]:
    """Spans per line, in file order, or None if this isn't a language we recognise."""
    lexer = _lexer_for(name, data)
    if lexer is None:
        return None
    style = get_style_by_name(DARK_STYLE if dark else LIGHT_STYLE)
    looks: dict = {}
    result = Highlighted(lexer.name)

    offset = 0
    starts = []
    for raw in data.split(b"\n"):
        starts.append(offset)
        offset += len(raw) + 1

    line = 0
    spans: list[Span] = []
    column = 0

    def finish() -> None:
        nonlocal line, spans, column
        if line < len(starts):
            result.lines[starts[line]] = spans or [Span("", None, False)]
        line, spans, column = line + 1, [], 0

    for token, text in lexer.get_tokens(data.decode("utf-8", "replace")):
        look = looks.get(token)
        if look is None:
            definition = style.style_for_token(token)
            color = definition["color"]
            look = looks[token] = (f"#{color}" if color else None, bool(definition["bold"]))
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            if index:
                finish()
            if piece:
                piece = _expand(piece, column) if expand_tabs else piece
                column += len(piece)
                if spans and spans[-1][1:] == look:
                    spans[-1] = Span(spans[-1].text + piece, *look)
                else:
                    spans.append(Span(piece, *look))
    finish()
    return result
