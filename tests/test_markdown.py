import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QTextDocument

from app.viewers.markdown import MAX_MARKDOWN_BYTES, MarkdownViewer
from app.viewers.text_log import TextLogViewer
from test_ui import Harness, pump, qapp  # noqa: F401

DOC = """# Title

Some **bold** text and a [link](https://example.com).

| a | b |
|---|---|
| 1 | 2 |

```python
print("hi")
```
"""


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def open_file(ui, name, kind):
    ui.window.list_model.reload()
    ui.select(name)
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, kind))
    return preview.current


def text(viewer):
    return viewer.browser.toPlainText()


def test_renders_and_toggles_to_source(ui, server):
    (server.root / "README.md").write_text(DOC)
    viewer = open_file(ui, "README.md", MarkdownViewer)
    assert pump(lambda: "Title" in text(viewer))
    assert "# Title" not in text(viewer) and "**" not in text(viewer)
    first = viewer.browser.document().firstBlock()
    assert first.blockFormat().headingLevel() == 1
    viewer.source.setChecked(True)
    assert "# Title" in text(viewer) and "**bold**" in text(viewer)
    viewer.source.setChecked(False)
    assert "**" not in text(viewer)


def test_reload_keeps_scroll_position(ui, server):
    path = server.root / "long.md"
    path.write_text("\n\n".join(f"para {i}" for i in range(2000)))
    viewer = open_file(ui, "long.md", MarkdownViewer)
    assert pump(lambda: "para 1999" in text(viewer))
    viewer.browser.resize(400, 200)
    bar = viewer.browser.verticalScrollBar()
    assert pump(lambda: bar.maximum() > 1000)
    bar.setValue(1000)
    pump(lambda: False, 0.6)  # polling baseline

    path.write_text("\n\n".join(f"new {i}" for i in range(2000)))
    later = time.time() + 2
    os.utime(path, (later, later))
    assert pump(lambda: "new 1999" in text(viewer))
    assert bar.value() == 1000


def test_remote_text_cannot_reach_local_files_or_odd_links(ui, server, tmp_path, monkeypatch):
    (server.root / "evil.md").write_text("![x](/etc/hostname)\n\n[l](file:///etc/passwd)\n")
    viewer = open_file(ui, "evil.md", MarkdownViewer)
    assert pump(lambda: viewer.browser.document().characterCount() > 1)
    resource = QTextDocument.ResourceType.ImageResource
    assert viewer.browser.loadResource(resource, QUrl("/etc/hostname")) is None

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", opened.append)
    viewer._link_clicked(QUrl("file:///etc/passwd"))
    viewer._link_clicked(QUrl("smb://server/share"))
    assert opened == []
    viewer._link_clicked(QUrl("https://example.com"))
    assert opened == [QUrl("https://example.com")]


def test_oversized_markdown_opens_as_text(ui, server):
    (server.root / "huge.md").write_bytes(b"line\n" * (MAX_MARKDOWN_BYTES // 5 + 1))
    open_file(ui, "huge.md", TextLogViewer)
