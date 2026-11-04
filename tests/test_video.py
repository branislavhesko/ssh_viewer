"""Real QMediaPlayer through the range server and SSH."""
import os
import subprocess
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import imageio_ffmpeg
import pytest
from PySide6.QtMultimedia import QMediaPlayer

from app.viewers.video import VideoViewer, mp4_is_playable
from test_ui import Harness, pump, qapp  # noqa: F401


def make_video(path, seconds=20, extra=()):
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=duration={seconds}:size=1280x720:rate=30",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-b:v", "8M",
         "-g", "30", *extra, str(path)], check=True)


@pytest.fixture
def ui(qapp, server, make_backend):
    harness = Harness(server, make_backend())
    yield harness
    harness.close()


def open_video(ui, name):
    ui.window.list_model.reload()
    ui.select(name)
    preview = ui.window.preview
    assert pump(lambda: isinstance(preview.current, VideoViewer))
    return preview.current


def test_plays_and_seeks(ui, server):
    path = server.root / "clip.mp4"
    make_video(path, extra=("-movflags", "+faststart"))
    viewer = open_video(ui, "clip.mp4")
    player = viewer.player.player
    assert pump(lambda: player.duration() > 19000, 15), player.errorString()
    assert pump(lambda: player.position() > 200, 15), (player.mediaStatus(), player.errorString())

    target = 15000
    viewer.player.seek(target)
    assert pump(lambda: player.position() >= target, 10), player.position()
    assert player.error() == QMediaPlayer.Error.NoError


def test_mkv_plays(ui, server):
    make_video(server.root / "clip.mkv", seconds=3)
    viewer = open_video(ui, "clip.mkv")
    player = viewer.player.player
    assert pump(lambda: player.position() > 100, 15), player.errorString()


def test_unfinished_plain_mp4_shows_banner_until_finished(ui, server):
    finished = server.root / "done.mp4"
    make_video(finished, seconds=2)  # moov at the end
    data = finished.read_bytes()
    moov = data.find(b"moov") - 4
    growing = server.root / "rec.mp4"
    growing.write_bytes(data[:moov])
    viewer = open_video(ui, "rec.mp4")
    assert pump(lambda: "still being written" in viewer.banner.text() and viewer.banner.isVisibleTo(viewer))
    pump(lambda: False, 0.6)
    growing.write_bytes(data)
    assert pump(lambda: not viewer.banner.isVisibleTo(viewer), 10)
    assert pump(lambda: viewer.player.player.position() > 50, 15)


def test_mp4_atom_walk(tmp_path):
    plain, frag = tmp_path / "a.mp4", tmp_path / "b.mp4"
    make_video(plain, seconds=1)
    make_video(frag, seconds=1, extra=("-movflags", "frag_keyframe+empty_moov"))
    for path in (plain, frag):
        data = path.read_bytes()
        assert mp4_is_playable(lambda o, n: data[o:o + n], len(data))
    data = plain.read_bytes()[:plain.read_bytes().find(b"moov") - 4]
    assert not mp4_is_playable(lambda o, n: data[o:o + n], len(data))
