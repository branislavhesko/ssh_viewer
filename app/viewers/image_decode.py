"""Decoding to numpy, and window/level down to 8-bit for display."""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
import tifffile
from PIL import Image
from PySide6.QtGui import QImage

TIFF_MAGIC = (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+")


@dataclass
class Decoded:
    pages: list[np.ndarray]  # each HxW, HxWx3 or HxWx4


def _normalize(array: np.ndarray) -> np.ndarray:
    array = np.squeeze(array)
    if array.ndim == 3 and array.shape[0] in (3, 4) and array.shape[2] not in (3, 4):
        array = np.moveaxis(array, 0, -1)  # planar RGB
    if array.ndim > 3 or (array.ndim == 3 and array.shape[2] not in (3, 4)):
        # A stack inside one page: show its first plane.
        array = array.reshape(-1, *array.shape[-2:])[0]
    if array.dtype == bool:
        array = array.astype(np.uint8) * 255
    return array


def decode(data: bytes) -> Decoded:
    if data[:4] in TIFF_MAGIC:
        with tifffile.TiffFile(io.BytesIO(data)) as tiff:
            return Decoded([_normalize(page.asarray()) for page in tiff.pages])
    with Image.open(io.BytesIO(data)) as image:
        pages = []
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            frame = image
            if image.mode in ("P", "PA", "CMYK", "YCbCr", "LAB", "HSV"):
                frame = image.convert("RGBA" if "transparency" in image.info else "RGB")
            elif image.mode == "LA":
                frame = image.convert("RGBA")
            pages.append(_normalize(np.array(frame)))
        return Decoded(pages)


def value_range(array: np.ndarray) -> tuple[float, float]:
    if array.dtype == np.uint8:
        return 0.0, 255.0
    finite = array[np.isfinite(array)] if array.dtype.kind == "f" else array
    if finite.size == 0:
        return 0.0, 1.0
    return float(finite.min()), float(finite.max())


def percentile_range(array: np.ndarray, low: float = 0.1, high: float = 99.9) -> tuple[float, float]:
    sample = array[::max(array.shape[0] // 1024, 1), ::max(array.shape[1] // 1024, 1)]
    if sample.ndim == 3:
        sample = sample[..., :3]
    sample = sample[np.isfinite(sample)] if sample.dtype.kind == "f" else sample
    if sample.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(sample, (low, high))
    return float(lo), float(hi)


def to_qimage(array: np.ndarray, low: float, high: float) -> QImage:
    if array.dtype == np.uint8 and low == 0 and high == 255:
        display = np.ascontiguousarray(array)
    else:
        span = max(high - low, np.finfo(np.float32).tiny)
        scaled = (array.astype(np.float32) - np.float32(low)) * np.float32(255.0 / span)
        display = np.nan_to_num(scaled, nan=0.0, posinf=255.0, neginf=0.0)
        display = np.ascontiguousarray(np.clip(display, 0, 255).astype(np.uint8))
        if array.ndim == 3 and array.shape[2] == 4:
            display[..., 3] = 255
    height, width = display.shape[:2]
    if display.ndim == 2:
        layout = QImage.Format.Format_Grayscale8
    elif display.shape[2] == 3:
        layout = QImage.Format.Format_RGB888
    else:
        layout = QImage.Format.Format_RGBA8888
    image = QImage(display.data, width, height, display.strides[0], layout)
    return image.copy()  # own the pixels; ``display`` goes away
