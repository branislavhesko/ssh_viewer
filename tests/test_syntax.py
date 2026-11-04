import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.backend import FileEntry
from app.viewers import syntax
from app.viewers.text_log import TextLogViewer, is_live_log
from test_ui import Harness, pump, qapp  # noqa: F401

CODE = b'''#!/usr/bin/env python3
import os

def main():
    """Docstring first line
    middle of the docstring
    """
    return 1  # done

\tindented = "tab"
'''


def color_of(spans, text):
    return next(s.color for s in spans if text in s.text)


def test_offsets_and_text_match_the_file():
    result = syntax.highlight("a.py", CODE, dark=False)
    assert result.language == "Python"
    offset = 0
    for raw in CODE.split(b"\n"):
        expected = raw.decode().expandtabs(8)
        assert "".join(s.text for s in result.lines[offset]) == expected
        offset += len(raw) + 1


def test_multiline_string_keeps_its_colour_on_inner_lines():
    result = syntax.highlight("a.py", CODE, dark=False)
    start = CODE.index(b"    middle")
    first = result.lines[CODE.index(b"    \"\"\"Docstring")]
    assert color_of(result.lines[start], "middle") == color_of(first, "Docstring")
    assert color_of(result.lines[start], "middle") is not None


def test_keywords_differ_from_names_and_themes_differ():
    light = syntax.highlight("a.py", CODE, dark=False).lines[CODE.index(b"def main")]
    dark = syntax.highlight("a.py", CODE, dark=True).lines[CODE.index(b"def main")]
    assert color_of(light, "def") != color_of(light, "main")
    assert color_of(light, "def") != color_of(dark, "def")


def test_multibyte_text_keeps_byte_offsets():
    data = "x = 'žluťoučký'\ny = 2\n".encode()
    result = syntax.highlight("a.py", data, dark=False)
    assert "".join(s.text for s in result.lines[data.index(b"y = 2")]) == "y = 2"


def test_leading_blank_lines_and_crlf_stay_aligned():
    data = b"\n\nimport os\r\nx = 1\r\n"
    result = syntax.highlight("a.py", data, dark=False)
    assert "".join(s.text for s in result.lines[data.index(b"import")]).startswith("import os")
    assert "".join(s.text for s in result.lines[data.index(b"x = 1")]).startswith("x = 1")


def test_language_detection():
    assert syntax.highlight("Makefile", b"all:\n\tcc\n", False).language == "Makefile"
    assert syntax.highlight("deploy", b"#!/bin/bash\necho hi\n", False) is not None
    assert syntax.highlight("notes.txt", b"plain\n", False) is None
    assert syntax.highlight("noext", b"plain\n", False) is None
    assert not syntax.worth_fetching("data.csvx")


def entry(name, age):
    return FileEntry(name, "/" + name, 1, time.time() - age, 0o100644, False)


def test_recently_saved_code_is_not_a_live_log():
    assert not is_live_log(entry("main.py", 5))
    assert is_live_log(entry("main.log", 99999))
    assert is_live_log(entry("output", 5))


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def open_text(ui, name):
    ui.window.list_model.reload()
    ui.select(name)
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, TextLogViewer))
    return preview.current


def rows(viewer):
    viewer.view.repaint()
    return viewer.view._rows


def colored(viewer):
    return any(s.color for row in rows(viewer) for s in row.spans)


def test_span_positions_match_how_the_font_really_lays_out_text(ui, server):
    from PySide6.QtGui import QFontMetricsF
    (server.root / "app.py").write_bytes(CODE)
    view = open_text(ui, "app.py").view
    line = "x" * 80
    measured = QFontMetricsF(view.font()).horizontalAdvance(line)
    assert abs(view._char_width * len(line) - measured) < 1.0


def test_viewer_colours_code_and_toggle_turns_it_off(ui, server):
    (server.root / "app.py").write_bytes(CODE)
    viewer = open_text(ui, "app.py")
    assert pump(lambda: colored(viewer))
    assert viewer.language.text() == "Python"
    assert rows(viewer)[0].text.startswith("#!/usr/bin")  # opens at the top
    viewer.colors.setChecked(False)
    assert not colored(viewer)
    viewer.colors.setChecked(True)
    assert colored(viewer)


def test_changed_file_is_recoloured_and_stale_lines_stay_plain(ui, server):
    path = server.root / "app.py"
    path.write_bytes(CODE)
    viewer = open_text(ui, "app.py")
    assert pump(lambda: colored(viewer))
    pump(lambda: False, 0.6)  # polling baseline

    # Before any re-tokenizing: the text no longer matches, so no colours on it.
    viewer.view.syntax = {0: [syntax.Span("something else", "#ff0000", False)]}
    assert all(s.color != "#ff0000" for s in rows(viewer)[0].spans)

    path.write_bytes(b"import sys\n" + CODE)
    later = time.time() + 2
    os.utime(path, (later, later))
    assert pump(lambda: rows(viewer) and rows(viewer)[0].text == "import sys"
                and any(s.color for s in rows(viewer)[0].spans))


def test_in_place_edit_of_same_size_shows_new_text_and_keeps_position(ui, server):
    path = server.root / "long.py"
    path.write_text("".join(f"value_{i:04} = 1\n" for i in range(3000)))
    viewer = open_text(ui, "long.py")
    assert pump(lambda: colored(viewer))
    viewer.view.resize(500, 200)
    viewer.view.jump_to(path.read_text().index("value_2000"))
    assert pump(lambda: rows(viewer) and rows(viewer)[0].text.startswith("value_2000"))
    pump(lambda: False, 0.6)  # polling baseline

    path.write_text("".join(f"other_{i:04} = 1\n" for i in range(3000)))
    later = time.time() + 2
    os.utime(path, (later, later))
    assert pump(lambda: rows(viewer) and rows(viewer)[0].text.startswith("other_2000"))
    assert pump(lambda: colored(viewer))


def test_oversized_code_stays_plain_without_fetching(ui, server):
    (server.root / "big.py").write_bytes(b"x = 1\n" * (syntax.MAX_HIGHLIGHT_BYTES // 6 + 1))
    viewer = open_text(ui, "big.py")
    assert pump(lambda: bool(rows(viewer)))
    pump(lambda: False, 0.3)
    assert not colored(viewer) and viewer.language.text() == ""
