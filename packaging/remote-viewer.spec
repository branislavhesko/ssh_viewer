# PyInstaller spec. Build from the repo root:
#   pyinstaller packaging/remote-viewer.spec
# Install rv_core first to bundle the Rust backend; without it the app uses the
# Python backend. PySide6 wheels carry Qt's FFmpeg, so video needs no system codecs.
import importlib.util

hidden = ["app.backend.py_backend"]
if importlib.util.find_spec("rv_core"):
    hidden += ["rv_core", "app.backend.rust_backend"]

analysis = Analysis(
    ["launcher.py"],
    pathex=[".."],
    hiddenimports=hidden,
    # Sent to the server as source when running Python, so it ships as a file.
    datas=[("../app/debug_agent.py", "app")],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick3D"],
)
pyz = PYZ(analysis.pure)
# PyInstaller turns the PNG into .icns or .ico (needs Pillow). Regenerate it with
#   python -m app.icon packaging/icon.png
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="remote-viewer", console=False,
          icon="icon.png")
collected = COLLECT(exe, analysis.binaries, analysis.datas, name="remote-viewer")
app = BUNDLE(collected, name="Remote Viewer.app", icon="icon.png",
             bundle_identifier="dev.remoteviewer.app")
