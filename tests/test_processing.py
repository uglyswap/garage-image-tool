import io

import cv2
import numpy as np
from PIL import Image

from app import processing
from app.plate_detector import PlateBox
from tests.helpers import jpeg_bytes, noisy_png_rgba


def test_retouch_preserves_png_alpha():
    src = noisy_png_rgba(120, 80, alpha_value=200)
    before = np.asarray(Image.open(io.BytesIO(src)).convert("RGBA"))
    out, fmt = processing.retouch_bytes(src, [[70, 10], [110, 10], [110, 60], [70, 60]], 3, "png", "blur")
    after = np.asarray(Image.open(io.BytesIO(out)).convert("RGBA"))
    assert fmt == "png"
    assert after.shape == before.shape
    assert np.array_equal(after[:, :, 3], before[:, :, 3])  # alpha intact
    assert not np.array_equal(after[10:60, 70:110, :3], before[10:60, 70:110, :3])  # zone floutée


def test_retouch_white_fills_polygon_and_keeps_jpeg():
    out, fmt = processing.retouch_bytes(jpeg_bytes(100, 100, (10, 10, 10)),
                                        [[20, 20], [80, 20], [80, 60], [20, 60]], 3, "jpeg", "white")
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert fmt == "jpeg"
    assert img[40, 50].min() > 200
    assert img[90, 5].max() < 40


def test_retouch_clamps_out_of_range_points():
    out, _ = processing.retouch_bytes(jpeg_bytes(100, 100), [[-500, -500], [9999, 0], [50, 9999]], 3, "jpeg", "blur")
    assert cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (100, 100)


def test_shift_plates_keeps_tilted_points():
    plate = PlateBox(10, 10, 50, 30, 0.9, [[10, 12], [50, 10], [50, 28], [10, 30]])
    (moved,) = processing._shift_plates([plate], 5, 20, 200, 100)
    assert (moved.x1, moved.y1, moved.x2, moved.y2) == (15, 30, 55, 50)
    assert moved.pts == [[15, 32], [55, 30], [55, 48], [15, 50]]


def test_shift_plates_clips_and_drops():
    inside_partial = PlateBox(-20, 10, 30, 40, 0.9)
    outside = PlateBox(10, 10, 50, 30, 0.9)
    result = processing._shift_plates([inside_partial, outside], 0, 200, 100, 100)
    assert result == []  # tout est sorti par le bas
    (clipped,) = processing._shift_plates([inside_partial], 0, 0, 100, 100)
    assert clipped.x1 == 0 and clipped.x2 == 30


def test_process_transparent_counts_plate_with_shift(monkeypatch):
    plate = PlateBox(20, 20, 80, 45, 0.9, [[20, 20], [80, 20], [80, 45], [20, 45]])
    monkeypatch.setattr(processing.detector, "detect", lambda bgr: [plate])

    def fake_remove(image, model=None):
        rgba = Image.new("RGBA", image.size, (0, 0, 0, 0))
        rgba.paste(Image.new("RGBA", (120, 60), (200, 50, 50, 255)), (10, 10))
        return rgba

    monkeypatch.setattr(processing, "_remove_background", fake_remove)
    result = processing.process_image(jpeg_bytes(200, 150), processing.ProcessOptions(bg_mode="transparent"))
    assert result.fmt == "png"
    assert result.plates_blurred == 1
