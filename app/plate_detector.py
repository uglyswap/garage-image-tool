"""Détection des plaques d'immatriculation.

Deux moteurs, par ordre de priorité :
1. YOLOv8 exporté en ONNX (multi-pays) — inférence onnxruntime, 100% CPU.
2. Cascade de Haar OpenCV (repli automatique si le modèle ONNX est absent).

Après détection, un affinage par contours resserre la boîte sur la plaque
réelle, pour ne flouter que la plaque — jamais la moitié de la voiture.

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


@dataclass
class PlateBox:
    x1: int
    y1: int
    x2: int
    y2: int
    score: float

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
        if self.engine == "yolo":
            boxes = self._detect_yolo(bgr)
            if config.PLATE_SMALL_PASS:
                boxes = self._merge_boxes(boxes, self._detect_small_pass(bgr))
        else:
            boxes = self._detect_haar(bgr)
        if config.PLATE_CLASSIC_PASS:
            boxes = self._merge_boxes(boxes, self._detect_classic(bgr, boxes))
        return [self._refine(bgr, b) for b in boxes]

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

    # ----------------------- affinage par contours ----------------------
    def _refine(self, bgr: np.ndarray, box: PlateBox) -> PlateBox:
        """Resserre la boîte sur les contours réels de la plaque.

        Dans la boîte détectée (avec une petite marge), on cherche le plus
        grand rectangle de ratio plausible (une plaque). S'il existe ET qu'il
        reste très proche de la détection initiale, on l'utilise ; sinon on
        garde la boîte du détecteur, déjà précise.
        """
        h0, w0 = bgr.shape[:2]
        margin_x, margin_y = int(box.w * 0.12), int(box.h * 0.25)
        rx1, ry1 = max(0, box.x1 - margin_x), max(0, box.y1 - margin_y)
        rx2, ry2 = min(w0, box.x2 + margin_x), min(h0, box.y2 + margin_y)
        roi = bgr[ry1:ry2, rx1:rx2]
        if roi.size == 0:
            return box
        gray = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        edges = cv2.dilate(cv2.Canny(gray, 60, 160), np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roi_area = float(roi.shape[0] * roi.shape[1])
        best = None
        best_area = 0.0
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < 8 or h < 6:
                continue
            ratio = w / float(h)
            if not 1.4 <= ratio <= 7.5:
                continue
            area = float(w * h)
            if area <= best_area or area < 0.20 * roi_area:
                continue
            best, best_area = (x, y, w, h), area
        if best is None:
            return box
        x, y, w, h = best
        nx1, ny1, nx2, ny2 = rx1 + x, ry1 + y, rx1 + x + w, ry1 + y + h
        ix1, iy1 = max(nx1, box.x1), max(ny1, box.y1)
        ix2, iy2 = min(nx2, box.x2), min(ny2, box.y2)
        inter = float(max(0, ix2 - ix1) * max(0, iy2 - iy1))
        union = float(box.w * box.h + w * h - inter)
        if union <= 0 or inter / union < 0.5 or (w * h) > 1.6 * float(box.w * box.h):
            return box
        return PlateBox(nx1, ny1, nx2, ny2, box.score)


# Singleton partagé (chargé au démarrage de l'app)
detector = PlateDetector()
