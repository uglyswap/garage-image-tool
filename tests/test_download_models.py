import hashlib
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "download_models", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "download_models.py")
dm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dm)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_fetch_accepts_matching_hash(tmp_path):
    src = tmp_path / "src.onnx"
    src.write_bytes(b"modele" * 100)
    dest = tmp_path / "dest.onnx"
    dm.fetch(src.as_uri(), str(dest), 10, _sha(src.read_bytes()), attempts=1)
    assert dest.read_bytes() == src.read_bytes()
    assert not (tmp_path / "dest.onnx.part").exists()


def test_fetch_rejects_tampered_file(tmp_path):
    src = tmp_path / "src.onnx"
    src.write_bytes(b"modele" * 100)
    dest = tmp_path / "dest.onnx"
    with pytest.raises(SystemExit):
        dm.fetch(src.as_uri(), str(dest), 10, _sha(b"autre contenu"), attempts=1)
    assert not dest.exists()
    assert not (tmp_path / "dest.onnx.part").exists()


def test_is_valid_checks_hash_and_size(tmp_path):
    f = tmp_path / "m.onnx"
    f.write_bytes(b"x" * 50)
    assert dm.is_valid(str(f), 10, _sha(b"x" * 50)) is True
    assert dm.is_valid(str(f), 10, _sha(b"y" * 50)) is False
    assert dm.is_valid(str(f), 100, _sha(b"x" * 50)) is False
    assert dm.is_valid(str(tmp_path / "absent"), 10, "0" * 64) is False


def test_pinned_hashes_are_well_formed():
    hashes = [sha for _u, sha in dm.PLATE_SOURCES] + [dm.U2NET[1], dm.BIREFNET_LITE[1], dm.ISNET[1]]
    assert all(len(h) == 64 and int(h, 16) >= 0 for h in hashes)
