"""Video over the local range server: QMediaPlayer, with libmpv as fallback."""
from __future__ import annotations

import struct
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSlider, QStackedLayout,
                               QVBoxLayout, QWidget)

from app.backend import Event, EventKind, FileEntry

from .base import Viewer

FRAME_STEP_MS = 40
MP4_SUFFIXES = (".mp4", ".m4v", ".mov")
MAX_ATOMS = 256


def mp4_is_playable(read: Callable[[int, int], bytes], size: int) -> bool:
    """Walk top-level atoms. Plain MP4 stores its index (moov) when recording
    finishes; fragmented MP4 has moof boxes as it goes."""
    offset = 0
    for _ in range(MAX_ATOMS):
        if offset + 8 > size:
            return False
        header = read(offset, 16)
        if len(header) < 8:
            return False
        length, kind = struct.unpack(">I4s", header[:8])
        if kind in (b"moov", b"moof"):
            return True
        if length == 1 and len(header) >= 16:
            length = struct.unpack(">Q", header[8:16])[0]
        if length < 8:
            return False  # length 0: a still-open box running to end of file
        offset += length
    return False


def format_time(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


class QtPlayer:
    """The small surface VideoViewer needs, so mpv can stand in."""

    def __init__(self, viewer: "VideoViewer"):
        self.widget = QVideoWidget()
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.widget)
        self.player.durationChanged.connect(viewer.duration_changed)
        self.player.positionChanged.connect(viewer.position_changed)
        self.player.playbackStateChanged.connect(
            lambda state: viewer.playing_changed(state == QMediaPlayer.PlaybackState.PlayingState))
        self.player.mediaStatusChanged.connect(self._status_changed)
        self.player.errorOccurred.connect(lambda _e, text: viewer.player_failed(text))
        self._viewer = viewer

    def _status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._viewer.reached_end()

    def load(self, url: str) -> None:
        self.player.setSource(QUrl(url))

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def seek(self, position: int) -> None:
        self.player.setPosition(position)

    def position(self) -> int:
        return self.player.position()

    def playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState


class MpvPlayer:
    """libmpv embedded by window id. Needs python-mpv and libmpv installed."""

    def __init__(self, viewer: "VideoViewer"):
        import mpv
        self.widget = QWidget()
        self.widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.mpv = mpv.MPV(wid=str(int(self.widget.winId())), keep_open="yes",
                           cache="yes", demuxer_readahead_secs=5)
        bridge = viewer.bridge
        self.mpv.observe_property("duration", lambda _n, value: value is not None and bridge.post(
            lambda: viewer.duration_changed(int(value * 1000))))
        self.mpv.observe_property("time-pos", lambda _n, value: value is not None and bridge.post(
            lambda: viewer.position_changed(int(value * 1000))))
        self.mpv.observe_property("pause", lambda _n, value: bridge.post(
            lambda: viewer.playing_changed(not value)))
        self.mpv.observe_property("eof-reached", lambda _n, value: value and bridge.post(
            viewer.reached_end))

    def load(self, url: str) -> None:
        self.mpv.play(url)

    def stop(self) -> None:
        self.mpv.stop()

    def play(self) -> None:
        self.mpv.pause = False

    def pause(self) -> None:
        self.mpv.pause = True

    def seek(self, position: int) -> None:
        self.mpv.seek(position / 1000, reference="absolute", precision="exact")

    def position(self) -> int:
        return int((self.mpv.time_pos or 0) * 1000)

    def playing(self) -> bool:
        return not self.mpv.pause


class VideoViewer(Viewer):
    def __init__(self, context, parent=None):
        super().__init__(context, parent)
        self._probe = ThreadPoolExecutor(1, thread_name_prefix="video-probe")
        self._screens = QStackedLayout()
        self.player = QtPlayer(self)
        self._screens.addWidget(self.player.widget)
        self._mpv_tried = False

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle)
        step = QPushButton("Frame ▶")
        step.clicked.connect(self._step)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.sliderMoved.connect(self._seek)
        self.slider.sliderReleased.connect(lambda: self.player.seek(self.slider.value()))
        self.time = QLabel("0:00:00 / 0:00:00")
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()

        bar = QHBoxLayout()
        bar.addWidget(self.play_button)
        bar.addWidget(step)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.banner)
        layout.addLayout(self._screens, 1)
        layout.addLayout(bar)
        self._duration = 0
        self._resume: Optional[int] = None

    # Loading -----------------------------------------------------------------
    def open(self, entry: FileEntry) -> None:
        super().open(entry)
        self.banner.hide()
        self._resume = None
        if entry.path.lower().endswith(MP4_SUFFIXES):
            self._check_mp4(entry)
        else:
            self._load()

    def close_file(self) -> None:
        super().close_file()
        self.player.stop()

    def _load(self) -> None:
        self.player.load(self.backend.open_url(self.entry.path))
        self.player.play()

    def _check_mp4(self, entry: FileEntry) -> None:
        current = self._current()

        def probe() -> bool:
            return mp4_is_playable(
                lambda offset, length: self.backend.read(entry.path, offset, length).result(30),
                entry.size)

        def done(playable: bool) -> None:
            if not current():
                return
            if playable:
                self.banner.hide()
                self._load()
            else:
                self.banner.setText("This MP4 is still being written, so it has no index yet. "
                                    "It becomes playable when finished. Fragmented MP4 or MKV "
                                    "can be watched while recording.")
                self.banner.show()
        self.bridge.then(self._probe.submit(probe), done, lambda _e: current() and self._load())

    def on_events(self, events: list[Event]) -> None:
        if self.banner.isVisible() and any(
                e.kind in (EventKind.CLOSE_WRITE, EventKind.MOVED_TO) for e in events):
            current = self._current()
            self.bridge.then(self.backend.stat(self.entry.path),
                             lambda entry: current() and self._check_mp4(entry))

    # Player callbacks --------------------------------------------------------
    def duration_changed(self, duration: int) -> None:
        self._duration = duration
        self.slider.setRange(0, duration)
        if self._resume is not None and duration:
            self.player.seek(min(self._resume, duration))
            self._resume = None
        self._update_time(self.player.position())

    def position_changed(self, position: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self._update_time(position)

    def _update_time(self, position: int) -> None:
        self.time.setText(f"{format_time(position)} / {format_time(self._duration)}")

    def playing_changed(self, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def reached_end(self) -> None:
        """A growing file may have more by now: pick up where we stopped."""
        if self.entry is None:
            return
        known, current = self.entry.size, self._current()

        def done(entry: FileEntry) -> None:
            if current() and entry.size > known:
                self.entry = entry
                self._resume = self._duration
                self.player.stop()
                self._load()
        self.bridge.then(self.backend.stat(self.entry.path), done)

    def player_failed(self, text: str) -> None:
        if self.entry is None or not text:
            return
        if self._mpv_tried:
            self.banner.setText(f"Playback failed: {text}")
            self.banner.show()
            return
        self._mpv_tried = True
        try:
            player = MpvPlayer(self)
        except (ImportError, OSError):
            self.banner.setText(f"Playback failed: {text}. Installing libmpv and python-mpv "
                                "adds a fallback player.")
            self.banner.show()
            return
        self.player.stop()
        self.player = player
        self._screens.addWidget(player.widget)
        self._screens.setCurrentWidget(player.widget)
        self._load()

    # Controls ----------------------------------------------------------------
    def _toggle(self) -> None:
        if self.player.playing():
            self.player.pause()
        else:
            self.player.play()

    def _seek(self, position: int) -> None:
        self.player.seek(position)

    def _step(self) -> None:
        self.player.pause()
        self.player.seek(self.player.position() + FRAME_STEP_MS)
