#!/usr/bin/env python3
"""Telecharge les modeles embarques (execute pendant le build Docker).

- u2net.onnx        : reseau de segmentation U2-Net utilise par rembg (~176 Mo)
- plate_yolov8.onnx : detecteur YOLOv8 de plaques d'immatriculation (~12 Mo)

Idempotent : ne rettelecharge pas un fichier deja present et valide.
"""
import os
import sys
import urllib.request

MODELS_DIR = os.environ.get("MODELS_DIR", "/app/models")

PLATE_URL = "https://huggingface.co/ml-debi/yolov8-license-plate-detection/resolve/main/best.onnx"
U2NET_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"


def fetch(url: str, dest: str, min_bytes: int, attempts: int = 3) -> None:
    last_err = None
    for i in range(1, attempts + 1):
        try:
            print("[download] (" + str(i) + "/" + str(attempts) + ") " + url, flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "garage-image-tool/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(dest + ".part", "wb") as f:
                total = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total % (32 << 20) < (1 << 20):
                        print("[download]   " + str(total // 1_000_000) + " Mo...", flush=True)
            size = os.path.getsize(dest + ".part")
            if size < min_bytes:
                raise RuntimeError("fichier trop petit (" + str(size) + " octets)")
            os.replace(dest + ".part", dest)
            print("[download] OK " + dest + " (" + str(size // 1_000_000) + " Mo)", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print("[download] echec : " + repr(exc), file=sys.stderr, flush=True)
    raise SystemExit("ERREUR : impossible de telecharger " + url + " : " + repr(last_err))


def main() -> int:
    os.makedirs(MODELS_DIR, exist_ok=True)
    plate = os.path.join(MODELS_DIR, "plate_yolov8.onnx")
    u2net = os.path.join(MODELS_DIR, "u2net.onnx")

    if os.path.isfile(plate) and os.path.getsize(plate) > 3_000_000:
        print("[download] plate_yolov8.onnx deja present")
    else:
        fetch(PLATE_URL, plate, 3_000_000)

    if os.path.isfile(u2net) and os.path.getsize(u2net) > 150_000_000:
        print("[download] u2net.onnx deja present")
    else:
        fetch(U2NET_URL, u2net, 150_000_000)

    print("[download] tous les modeles sont prets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
