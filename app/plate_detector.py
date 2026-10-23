"""Détection des plaques d'immatriculation.

Deux moteurs, par ordre de priorité :
1. YOLOv8 exporté en ONNX (multi-pays) : inférence onnxruntime, 100% CPU.
2. Cascade de Haar OpenCV (repli automatique si le modèle ONNX est absent).

Après détection, un affinage par contours resserre la boîte sur la plaque
réelle, pour ne flouter que la plaque, jamais la moitié de la voiture.

Le détecteur est pays-agnostique : il détecte la *géométrie* du rectangle
de plaque, pas son texte (fonctionne UE, US, Asie, motos, remorques...).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import cv2
import numpy as np

from . import config

logger = logging.getLogger(__name__)

INPUT_SIZE = 640
QUAD_MARGIN = 0.03        # marge de recherche du contour autour de la boîte YOLO
QUAD_MIN_AREA = 0.30      # aire mini du contour, en part de la boîte YOLO (écarte emblèmes et caractères)
SHRINK_FALLBACK = 0.03    # réduction de la boîte YOLO quand aucun contour de plaque n'est trouvé


@dataclass
class PlateBox:
    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    pts: list = None  # 4 coins [x,y] du quadrilatère de la plaque (incliné ou en perspective)

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1


class PlateDetector:
    """Détection de plaques. Thread-safe (les sessions onnxruntime le sont)."""

    def __init__(self, model_path: str | None = None, conf_threshold: float | None = None):
        self.conf = conf_threshold if conf_threshold is not None else config.PLATE_CONF_THRESHOLD
        path = model_path or config.PLATE_MODEL_PATH
        self.engine = "haar"
        self.session = None
        self.input_name = None
        self.model_file = None
        if path and os.path.isfile(path):
            try:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.log_severity_level = 3
                self.session = ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])
                self.input_name = self.session.get_inputs()[0].name
                self.engine = "yolo"
                self.model_file = os.path.basename(path)
                logger.info("Détecteur de plaques : YOLO ONNX (%s)", path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Échec du chargement YOLO ONNX (%s) : %s -> repli Haar", path, exc)
        if self.engine == "haar":
            cascade_file = os.path.join(cv2.data.haarcascades, "haarcascade_russian_plate_number.xml")
            self.cascade = cv2.CascadeClassifier(cascade_file)
            logger.info("Détecteur de plaques : cascade de Haar (repli)")

    # ------------------------------------------------------------------
    def detect(self, bgr: np.ndarray) -> list[PlateBox]:
        """Détecte les plaques dans une image BGR (OpenCV)."""
        weak: list[PlateBox] = []
        if self.engine == "yolo":
            weak_thr = config.PLATE_WEAK_THRESHOLD
            low = min(self.conf, weak_thr) if weak_thr > 0 else self.conf
            found = self._detect_yolo(bgr, conf=low)
            boxes = [b for b in found if b.score >= self.conf]
            weak = [b for b in found if b.score < self.conf]
            if config.PLATE_SMALL_PASS:
                boxes = self._merge_boxes(boxes, self._detect_small_pass(bgr))
        else:
            boxes = self._detect_haar(bgr)
        if config.PLATE_CLASSIC_PASS:
            boxes = self._merge_boxes(boxes, self._detect_classic(bgr, boxes))
        refined = [self._refine(bgr, b) for b in boxes]
        # Détections faibles (plaque petite ou floue) : retenues seulement si un contour de plaque net est trouvé
        for b in weak:
            quad = self._fit_quad(bgr, b)
            if quad is not None and not any(self._overlaps(quad, r) for r in refined):
                refined.append(quad)
        return refined

    @staticmethod
    def _overlaps(a: PlateBox, b: PlateBox) -> bool:
        ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
        iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
        return ix * iy > 0.3 * min(a.w * a.h, b.w * b.h)

    def _detect_classic(self, bgr, existing):
        """Passe de sauvetage classique (sans IA) : les modeles YOLO ratent
        parfois les plaques inclinees/ombrees. Recherche de rectangles clairs,
        au ratio de plaque, contenant du texte (fort contraste interne)."""
        h0, w0 = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        seuil = max(140.0, float(np.percentile(gray, 80)))
        bright = ((gray > seuil) * 255).astype(np.uint8)
        bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((15, 5), np.uint8))
        contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cands = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < 45 or h < 16 or w > 0.45 * w0:
                continue
            ratio = w / float(h)
            if not 1.6 <= ratio <= 7.0:
                continue
            roi = gray[y : y + h, x : x + w]
            if float(roi.mean()) < 135.0:
                continue
            cy1, cy2 = int(h * 0.2), int(h * 0.8)
            cx1, cx2 = int(w * 0.1), int(w * 0.9)
            inner = roi[cy1:cy2, cx1:cx2]
            if float(inner.std()) < 42.0:
                continue
            box = (x, y, x + w, y + h)
            overlap = False
            for e in existing:
                ix1, iy1 = max(box[0], e.x1), max(box[1], e.y1)
                ix2, iy2 = min(box[2], e.x2), min(box[3], e.y2)
                inter = float(max(0, ix2 - ix1) * max(0, iy2 - iy1))
                a1 = float((box[2] - box[0]) * (box[3] - box[1]))
                a2 = float((e.x2 - e.x1) * (e.y2 - e.y1))
                if a1 + a2 - inter > 0 and inter / (a1 + a2 - inter) > 0.25:
                    overlap = True
                    break
            if not overlap:
                cands.append(PlateBox(x, y, x + w, y + h, 0.55))
        return cands

    def _detect_small_pass(self, bgr: np.ndarray) -> list[PlateBox]:
        """2e passe : grille 2x2 de tuiles agrandies pour attraper les plaques
        lointaines / petites (voitures de fond, photos prises de loin)."""
        h, w = bgr.shape[:2]
        if w < 1100 or h < 800:
            return []
        found: list[PlateBox] = []
        ow, oh = w // 2, h // 2
        qx, qy = ow // 4, oh // 4
        for (ox, oy) in ((0, 0), (ow - qx, 0), (0, oh - qy), (ow - qx, oh - qy)):
            tile = bgr[oy:oy + oh + qy, ox:ox + ow + qx]
            th, tw = tile.shape[:2]
            if tw < 400 or th < 300:
                continue
            scale = min(2.0, 3200.0 / tw)
            big = cv2.resize(tile, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            # Seuil plus haut sur les tuiles : l'agrandissement crée des faux positifs
            for b in self._detect_yolo(big, conf=max(0.5, self.conf)):
                found.append(PlateBox(
                    int(ox + b.x1 / scale), int(oy + b.y1 / scale),
                    int(ox + b.x2 / scale), int(oy + b.y2 / scale), b.score))
        return found

    @staticmethod
    def _merge_boxes(a: list[PlateBox], b: list[PlateBox]) -> list[PlateBox]:
        """Fusionne les détections pleine image + tuiles (dé-dupliquées par NMS)."""
        if not b:
            return a
        allb = a + b
        xyxy = np.array([[x.x1, x.y1, x.x2, x.y2] for x in allb], dtype=np.float32)
        scores = np.array([x.score for x in allb], dtype=np.float32)
        keep = PlateDetector._nms(xyxy, scores, iou_thr=0.4)
        return [allb[i] for i in keep]

    # ---------------------------- YOLO ONNX ----------------------------
    def _detect_yolo(self, bgr: np.ndarray, conf: float | None = None) -> list[PlateBox]:
        h0, w0 = bgr.shape[:2]
        conf_threshold = conf if conf is not None else self.conf
        scale, pad_x, pad_y, inp = self._letterbox(bgr)
        blob = (inp.astype(np.float32) / 255.0).transpose(2, 0, 1)[np.newaxis]
        pred = self.session.run(None, {self.input_name: blob})[0]
        pred = np.squeeze(pred, axis=0)
        # Layout standard YOLOv8/v11 exporté : (4+nc, N) -> on transpose en (N, 4+nc)
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T
        num_classes = pred.shape[1] - 4
        boxes_cxcywh = pred[:, :4]
        scores = pred[:, 4:].max(axis=1) if num_classes > 1 else pred[:, 4]
        keep = scores >= conf_threshold
        boxes_cxcywh, scores = boxes_cxcywh[keep], scores[keep]
        if boxes_cxcywh.size == 0:
            return []
        xyxy = np.empty_like(boxes_cxcywh)
        xyxy[:, 0] = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        xyxy[:, 1] = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        xyxy[:, 2] = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        xyxy[:, 3] = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
        out: list[PlateBox] = []
        for i in self._nms(xyxy, scores, iou_thr=0.45):
            x1 = (xyxy[i, 0] - pad_x) / scale
            y1 = (xyxy[i, 1] - pad_y) / scale
            x2 = (xyxy[i, 2] - pad_x) / scale
            y2 = (xyxy[i, 3] - pad_y) / scale
            box = self._clip_box(PlateBox(int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2)), float(scores[i])), w0, h0)
            if box is not None:
                out.append(box)
        return out

    def _detect_haar(self, bgr: np.ndarray) -> list[PlateBox]:
        h0, w0 = bgr.shape[:2]
        gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        min_w = max(48, int(w0 * 0.02))
        detections = self.cascade.detectMultiScale(
            gray, scaleFactor=1.06, minNeighbors=5, minSize=(min_w, max(16, min_w // 3))
        )
        out: list[PlateBox] = []
        for (x, y, w, h) in detections:
            box = self._clip_box(PlateBox(int(x), int(y), int(x + w), int(y + h), 0.99), w0, h0)
            if box is not None:
                out.append(box)
        return out

    # --------------------------- utilitaires ---------------------------
    @staticmethod
    def _letterbox(bgr: np.ndarray):
        h0, w0 = bgr.shape[:2]
        scale = min(INPUT_SIZE / w0, INPUT_SIZE / h0)
        nw, nh = int(round(w0 * scale)), int(round(h0 * scale))
        resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        pad_x, pad_y = (INPUT_SIZE - nw) // 2, (INPUT_SIZE - nh) // 2
        canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
        canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = resized
        return scale, pad_x, pad_y, canvas

    @staticmethod
    def _nms(xyxy: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
        x1, y1, x2, y2 = xyxy[:, 0], xyxy[:, 1], xyxy[:, 2], xyxy[:, 3]
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size > 0:
            i = int(order[0])
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            iou = inter / np.maximum(areas[i] + areas[order[1:]] - inter, 1e-9)
            order = order[1:][iou <= iou_thr]
        return keep

    @staticmethod
    def _clip_box(box: PlateBox, w: int, h: int) -> PlateBox | None:
        x1, y1 = max(0, box.x1), max(0, box.y1)
        x2, y2 = min(w, box.x2), min(h, box.y2)
        if x2 - x1 < 12 or y2 - y1 < 8:
            return None
        ratio = (x2 - x1) / max(1, y2 - y1)
        if not 1.2 <= ratio <= 9.0:
            return None
        box.x1, box.y1, box.x2, box.y2 = x1, y1, x2, y2
        return box

    # ------------------- ajustement sur le contour réel -------------------
    def _refine(self, bgr: np.ndarray, box: PlateBox) -> PlateBox:
        """Resserre la boîte YOLO sur la plaque.

        Quadrilatère exact (incliné, éventuellement en perspective) si un contour
        fermé de plaque est trouvé ; sinon la boîte du détecteur légèrement réduite
        (elle déborde toujours un peu de la plaque)."""
        quad = self._fit_quad(bgr, box)
        return quad if quad is not None else self._shrink(box, SHRINK_FALLBACK)

    @staticmethod
    def _shrink(box: PlateBox, ratio: float) -> PlateBox:
        dx, dy = int(box.w * ratio), int(box.h * ratio)
        return PlateBox(box.x1 + dx, box.y1 + dy, box.x2 - dx, box.y2 - dy, box.score)

    @staticmethod
    def _order_corners(pts: np.ndarray) -> np.ndarray:
        """Ordonne 4 points : haut-gauche, haut-droite, bas-droite, bas-gauche."""
        s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
        return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype=np.float32)

    def _as_quad(self, cnt: np.ndarray) -> np.ndarray | None:
        """Quadrilatère convexe d'un contour : 4 sommets de l'enveloppe, sinon rectangle orienté très rempli."""
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area <= 0:
            return None
        peri = cv2.arcLength(hull, True)
        for eps in (0.02, 0.03, 0.04, 0.06):
            approx = cv2.approxPolyDP(hull, eps * peri, True)
            if len(approx) == 4:
                return self._order_corners(approx.reshape(4, 2).astype(np.float32))
        rect = cv2.minAreaRect(hull)
        rect_area = rect[1][0] * rect[1][1]
        if rect_area > 0 and hull_area / rect_area >= 0.9:
            return self._order_corners(cv2.boxPoints(rect))
        return None

    @staticmethod
    def _quad_is_plate(quad: np.ndarray) -> bool:
        """Proportions de plaque, côtés opposés comparables (perspective modérée)."""
        tl, tr, br, bl = quad
        top, bot = float(np.linalg.norm(tr - tl)), float(np.linalg.norm(br - bl))
        lef, rig = float(np.linalg.norm(bl - tl)), float(np.linalg.norm(br - tr))
        if min(top, bot, lef, rig) < 4:
            return False
        if max(top, bot) / min(top, bot) > 1.5 or max(lef, rig) / min(lef, rig) > 1.5:
            return False
        return 1.6 <= (top + bot) / (lef + rig) <= 9.0

    def _fit_quad(self, bgr: np.ndarray, box: PlateBox) -> PlateBox | None:
        """Cherche, dans la boîte YOLO, le plus grand contour fermé à 4 coins ayant des proportions de plaque."""
        h0, w0 = bgr.shape[:2]
        mx, my = max(2, int(box.w * QUAD_MARGIN)), max(2, int(box.h * QUAD_MARGIN))
        rx1, ry1 = max(0, box.x1 - mx), max(0, box.y1 - my)
        rx2, ry2 = min(w0, box.x2 + mx), min(h0, box.y2 + my)
        roi = bgr[ry1:ry2, rx1:rx2]
        rh, rw = roi.shape[:2]
        if rh < 8 or rw < 12:
            return None
        k = max(3, (box.h // 50) | 1)
        gray = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (k, k), 0)
        med = float(np.median(gray))
        edges = cv2.Canny(gray, int(max(10, 0.5 * med)), int(max(40, 1.2 * med)))
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        box_area = float(box.w * box.h)
        cx1, cy1, cx2, cy2 = box.x1 - rx1, box.y1 - ry1, box.x2 - rx1, box.y2 - ry1
        best, best_area = None, 0.0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area <= best_area or area < QUAD_MIN_AREA * box_area or area > 1.05 * box_area:
                continue
            quad = self._as_quad(cnt)
            if quad is None or not self._quad_is_plate(quad):
                continue
            if quad.min() < 1 or quad[:, 0].max() > rw - 2 or quad[:, 1].max() > rh - 2:
                continue  # contour coupé par le bord de la zone de recherche
            cx, cy = float(quad[:, 0].mean()), float(quad[:, 1].mean())
            if not (cx1 + 0.2 * box.w <= cx <= cx2 - 0.2 * box.w and cy1 + 0.2 * box.h <= cy <= cy2 - 0.2 * box.h):
                continue
            best, best_area = quad, area
        if best is None:
            return None
        pts = [[float(x) + rx1, float(y) + ry1] for x, y in best]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return PlateBox(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)), box.score, pts)


# Singleton partagé (chargé au démarrage de l'app)
detector = PlateDetector()
