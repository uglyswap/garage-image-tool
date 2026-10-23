import cv2
import numpy as np

from app import processing
from app.plate_detector import PlateBox, PlateDetector


def _detector() -> PlateDetector:
    return PlateDetector(model_path="/introuvable")  # repli Haar : aucun modèle ONNX requis


def _scene(corners, size=(900, 600)):
    """Fond clair uni, plaque blanche à bordure sombre dessinée sur les coins donnés."""
    img = np.full((size[1], size[0], 3), 200, np.uint8)
    poly = np.array(corners, np.int32)
    cv2.fillPoly(img, [poly], (245, 245, 245))
    cv2.polylines(img, [poly], True, (30, 30, 30), 4)
    cv2.putText(img, "AB-123-CD", (int(poly[:, 0].min()) + 40, int(poly[:, 1].mean()) + 20),
                cv2.FONT_HERSHEY_SIMPLEX, 2.0, (20, 20, 20), 5)
    return img


def _yolo_box_around(corners, margin=0.06):
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    return PlateBox(int(min(xs) - w * margin), int(min(ys) - h * margin),
                    int(max(xs) + w * margin), int(max(ys) + h * margin), 0.8)


def _max_corner_error(found, expected):
    return max(min(np.hypot(p[0] - e[0], p[1] - e[1]) for p in found) for e in expected)


def test_fit_quad_tilted_plate_follows_the_real_corners():
    corners = [(220, 250), (640, 300), (630, 420), (210, 370)]  # plaque inclinée d'environ 7 degrés
    img = _scene(corners)
    quad = _detector()._fit_quad(img, _yolo_box_around(corners))
    assert quad is not None and quad.pts is not None
    assert _max_corner_error(quad.pts, corners) < 8


def test_fit_quad_handles_perspective_trapezoid():
    corners = [(220, 240), (660, 270), (650, 390), (235, 375)]  # côté gauche plus haut que le droit
    img = _scene(corners)
    quad = _detector()._fit_quad(img, _yolo_box_around(corners))
    assert quad is not None
    assert _max_corner_error(quad.pts, corners) < 8


def test_refine_without_plate_contour_shrinks_the_box_instead_of_growing_it():
    img = np.full((400, 600, 3), 150, np.uint8)  # rien à trouver
    box = PlateBox(100, 100, 400, 200, 0.7)
    out = _detector()._refine(img, box)
    assert out.pts is None
    assert out.x1 > box.x1 and out.y1 > box.y1 and out.x2 < box.x2 and out.y2 < box.y2


def test_refine_never_returns_something_larger_than_the_detector_box():
    corners = [(220, 250), (640, 300), (630, 420), (210, 370)]
    img = _scene(corners)
    box = _yolo_box_around(corners)
    out = _detector()._refine(img, box)
    assert out.x1 >= box.x1 and out.y1 >= box.y1 and out.x2 <= box.x2 and out.y2 <= box.y2


def test_mask_hugs_a_tilted_plate():
    corners = [(220, 250), (640, 300), (630, 420), (210, 370)]
    box = PlateBox(210, 250, 640, 420, 0.8, [list(map(float, c)) for c in corners])
    mask = processing._plate_mask((600, 900, 3), box)
    quad_area = cv2.contourArea(np.array(corners, np.float32))
    covered = float((mask > 127).sum())
    assert quad_area <= covered <= quad_area * 1.25  # marge fine, très loin du rectangle englobant
    assert covered < 0.85 * box.w * box.h


def test_weak_detection_without_contour_is_dropped(monkeypatch):
    det = _detector()
    det.engine = "yolo"
    img = np.full((400, 600, 3), 150, np.uint8)
    weak = PlateBox(100, 100, 300, 160, 0.25)
    monkeypatch.setattr(det, "_detect_yolo", lambda bgr, conf=None: [weak])
    monkeypatch.setattr("app.config.PLATE_SMALL_PASS", False)
    assert det.detect(img) == []
