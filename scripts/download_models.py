#!/usr/bin/env python3
"""Telecharge les modeles embarques (execute pendant le build Docker).

- plate_yolov8.onnx          : detecteur YOLO de plaques d'immatriculation (~38 Mo)
- isnet-general-use.onnx     : detourage rapide (~178 Mo)
- birefnet-general-lite.onnx : detourage qualite max (~224 Mo)
- u2net.onnx                 : U2-Net, modele de repli de rembg (~176 Mo)

Chaque fichier est verifie par SHA-256 (ces modeles sont executes par onnxruntime) :
un fichier dont l'empreinte differe est refuse. Si une source distante change
legitimement, mettre a jour l'empreinte ci-dessous (sha256sum du nouveau fichier).

Idempotent : ne rettelecharge pas un fichier deja present et valide.
"""
import hashlib
import os
import sys
import urllib.request

MODELS_DIR = os.environ.get("MODELS_DIR", "/app/models")

# (url, sha256) par ordre de priorite
PLATE_SOURCES = [
    # Modèle principal : YOLO11s plaques (morsetechlab, ~72k téléchargements HF)
    ("https://huggingface.co/morsetechlab/yolov11-license-plate-detection/resolve/main/license-plate-finetune-v1s.onnx",
     "301e4239442b3a533c868ad1cc2ec38933e75ca53e5c94c58755cff2cfb294b7"),
    # Repli : YOLOv8n plaques (ml-debi)
    ("https://huggingface.co/ml-debi/yolov8-license-plate-detection/resolve/main/best.onnx",
     "85d236280a1301ad98907947d284951dd2b20c23a6786ff50f7e6a8ec515bd50"),
]
U2NET = ("https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx",
         "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491")
BIREFNET_LITE = ("https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
                 "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333")  # qualite Max
ISNET = ("https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
         "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a")  # detourage rapide


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def is_valid(path: str, min_bytes: int, sha256: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > min_bytes and sha256_of(path) == sha256


def fetch(url: str, dest: str, min_bytes: int, sha256: str, attempts: int = 3) -> None:
    last_err = None
    for i in range(1, attempts + 1):
        try:
            print("[download] (" + str(i) + "/" + str(attempts) + ") " + url, flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "garage-image-tool/1.1"})
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
            actual = sha256_of(dest + ".part")
            if actual != sha256:
                raise RuntimeError("empreinte SHA-256 inattendue : " + actual)
            os.replace(dest + ".part", dest)
            print("[download] OK " + dest + " (" + str(size // 1_000_000) + " Mo, sha256 verifie)", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print("[download] echec : " + repr(exc), file=sys.stderr, flush=True)
            try:
                os.remove(dest + ".part")
            except OSError:
                pass
    raise SystemExit("ERREUR : impossible de telecharger " + url + " : " + repr(last_err))


def ensure(name: str, source: tuple, min_bytes: int) -> None:
    dest = os.path.join(MODELS_DIR, name)
    url, sha256 = source
    if is_valid(dest, min_bytes, sha256):
        print("[download] " + name + " deja present (sha256 verifie)")
    else:
        fetch(url, dest, min_bytes, sha256)


def main() -> int:
    os.makedirs(MODELS_DIR, exist_ok=True)
    plate = os.path.join(MODELS_DIR, "plate_yolov8.onnx")

    if any(is_valid(plate, 3_000_000, sha) for _url, sha in PLATE_SOURCES):
        print("[download] plate_yolov8.onnx deja present (sha256 verifie)")
    else:
        last_err = None
        for url, sha in PLATE_SOURCES:
            try:
                fetch(url, plate, 3_000_000, sha, attempts=2)
                last_err = None
                break
            except SystemExit as exc:
                last_err = exc
                print("[download] source echouee, essai suivant...", file=sys.stderr, flush=True)
        if last_err:
            raise last_err

    ensure("isnet-general-use.onnx", ISNET, 150_000_000)
    ensure("birefnet-general-lite.onnx", BIREFNET_LITE, 150_000_000)
    ensure("u2net.onnx", U2NET, 150_000_000)

    print("[download] tous les modeles sont prets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
