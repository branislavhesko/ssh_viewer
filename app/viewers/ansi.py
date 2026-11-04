"""SGR colour parsing for log lines."""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")

PALETTE = ("#000000", "#cc3333", "#33aa33", "#b58900", "#3366cc", "#aa33aa", "#2aa198", "#cccccc")
BRIGHT = ("#666666", "#ff5555", "#55dd55", "#e8c33a", "#6699ff", "#ee66ee", "#55dddd", "#ffffff")


class Span(NamedTuple):
    text: str
    color: Optional[str]
    bold: bool


def _xterm(index: int) -> str:
    if index < 8:
        return PALETTE[index]
    if index < 16:
        return BRIGHT[index - 8]
    if index < 232:
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return "#%02x%02x%02x" % (steps[index // 36], steps[index // 6 % 6], steps[index % 6])
    level = 8 + (index - 232) * 10
    return "#%02x%02x%02x" % (level, level, level)


def _apply(codes: list[int], color: Optional[str], bold: bool) -> tuple[Optional[str], bool]:
    i = 0
    while i < len(codes):
        code = codes[i]
        if code == 0:
            color, bold = None, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif code == 39:
            color = None
        elif 30 <= code <= 37:
            color = PALETTE[code - 30]
        elif 90 <= code <= 97:
            color = BRIGHT[code - 90]
        elif code == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
            color = _xterm(min(codes[i + 2], 255))
            i += 2
        elif code == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
            color = "#%02x%02x%02x" % tuple(min(c, 255) for c in codes[i + 2:i + 5])
            i += 4
        i += 1
    return color, bold


def parse(line: str) -> list[Span]:
    """Split a line into styled spans, dropping all escape sequences."""
    if "\x1b" not in line:
        return [Span(line, None, False)]
    line = _OTHER_ESCAPE.sub("", line)
    spans, color, bold, position = [], None, False, 0
    for match in _SGR.finditer(line):
        if match.start() > position:
            spans.append(Span(line[position:match.start()], color, bold))
        codes = [int(c) if c else 0 for c in match[1].split(";")]
        color, bold = _apply(codes, color, bold)
        position = match.end()
    if position < len(line):
        spans.append(Span(line[position:], color, bold))
    return spans or [Span("", None, False)]


def strip(line: str) -> str:
    return "".join(span.text for span in parse(line))
