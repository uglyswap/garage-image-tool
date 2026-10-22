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

PLATE_URLS = [
    # Modèle principal : YOLO11s plaques (morsetechlab, ~72k téléchargements HF)
    "https://huggingface.co/morsetechlab/yolov11-license-plate-detection/resolve/main/license-plate-finetune-v1s.onnx",
    # Repli : YOLOv8n plaques (ml-debi)
    "https://huggingface.co/ml-debi/yolov8-license-plate-detection/resolve/main/best.onnx",
]
U2NET_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
BIREFNET_LITE_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx"  # qualite Max
ISNET_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"  # modèle principal détourage


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
        last_err = None
        for url in PLATE_URLS:
            try:
                fetch(url, plate, 3_000_000, attempts=2)
                last_err = None
                break
            except SystemExit as exc:
                last_err = exc
                print("[download] source echouee, essai suivant...", file=sys.stderr, flush=True)
        if last_err:
            raise last_err

    isnet = os.path.join(MODELS_DIR, "isnet-general-use.onnx")
    if os.path.isfile(isnet) and os.path.getsize(isnet) > 150_000_000:
        print("[download] isnet-general-use.onnx deja present")
    else:
        fetch(ISNET_URL, isnet, 150_000_000)

    birefnet = os.path.join(MODELS_DIR, "birefnet-general-lite.onnx")
    if os.path.isfile(birefnet) and os.path.getsize(birefnet) > 150_000_000:
        print("[download] birefnet-general-lite.onnx deja present")
    else:
        fetch(BIREFNET_LITE_URL, birefnet, 150_000_000)

    if os.path.isfile(u2net) and os.path.getsize(u2net) > 150_000_000:
        print("[download] u2net.onnx deja present")
    else:
        fetch(U2NET_URL, u2net, 150_000_000)

    print("[download] tous les modeles sont prets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
