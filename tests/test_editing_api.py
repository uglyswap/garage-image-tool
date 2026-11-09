"""API d'édition après traitement : réglages, aperçu, annulation, exports, ZIP, en-têtes."""
import io
import json
import os
import time
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, processing
from tests.helpers import jpeg_bytes


@pytest.fixture()
def client():
    return TestClient(main.app)


def _job(client, n=1, **form):
    data = {"bg_mode": "original", "out_format": "jpeg", "blur_enabled": "false"}
    data.update(form)
    files = [("files", ("photo.jpg", jpeg_bytes(color=(120, 90, 60)), "image/jpeg")) for _ in range(n)]
    job_id = client.post("/api/jobs", data=data, files=files).json()["job_id"]
    end = time.time() + 30
    while time.time() < end:
        body = client.get("/api/jobs/" + job_id).json()
        if body["status"] == "done":
            return job_id, body
        time.sleep(0.05)
    raise AssertionError("job non terminé")


def _mean(content):
    return np.asarray(Image.open(io.BytesIO(content)).convert("RGB")).mean()


def test_job_keeps_master_thumb_and_exposes_edit_state(client):
    job_id, body = _job(client)
    names = os.listdir(main.jobs.job_dir(job_id))
    assert {"master_0.jpg", "out_0.jpg", "th_0.webp"} <= set(names)
    r = body["results"][0]
    assert r["rev"] == 1 and r["edits"] == 0 and r["fg_only"] is False
    assert r["adjust"] == {"brightness": 0, "contrast": 0, "saturation": 0, "vibrance": 0}
    thumb = client.get(r["url"] + "?thumb=1")
    assert thumb.headers["content-type"] == "image/webp"
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= processing.THUMB_SIDE


def test_adjust_is_applied_persisted_and_reversible(client):
    job_id, body = _job(client)
    url = body["results"][0]["url"]
    before = client.get(url).content
    r = client.put(url + "/adjust", json={"brightness": 60})
    assert r.status_code == 200 and r.json()["rev"] == 2 and r.json()["adjust"]["brightness"] == 60
    assert _mean(client.get(url).content) > _mean(before) + 10
    with open(os.path.join(main.jobs.job_dir(job_id), "status.json"), encoding="utf-8") as f:
        assert json.load(f)["results"][0]["adjust"]["brightness"] == 60  # survit à un redémarrage
    client.put(url + "/adjust", json={})
    assert abs(_mean(client.get(url).content) - _mean(before)) < 1  # retour à l'image d'origine, sans perte


@pytest.mark.parametrize("payload", [{"brightness": 101}, {"contrast": -101}, {"vibrance": "x"}])
def test_adjust_bounds(client, payload):
    _, body = _job(client)
    assert client.put(body["results"][0]["url"] + "/adjust", json=payload).status_code == 422


def test_preview_matches_requested_settings_without_saving(client):
    _, body = _job(client)
    url = body["results"][0]["url"]
    plain = client.get(url + "/preview")
    darker = client.get(url + "/preview?brightness=-80")
    assert plain.status_code == 200 and plain.headers["cache-control"] == "no-store"
    assert _mean(darker.content) < _mean(plain.content) - 10
    assert client.get("/api/jobs/" + body["job_id"]).json()["results"][0]["rev"] == 1
    assert client.get(url + "/preview?brightness=500").status_code == 422


def test_retouch_is_undoable(client):
    _, body = _job(client)
    url = "/api/retouch/" + body["job_id"] + "/0"
    result = body["results"][0]["url"]
    before = client.get(result).content
    poly = {"polygon": [[10, 10], [200, 10], [200, 150], [10, 150]], "style": "white"}
    assert client.post(url, json=poly).json()["edits"] == 1
    assert client.post(url, json=poly).json()["edits"] == 2
    assert _mean(client.get(result).content) > _mean(before) + 20
    assert client.delete(url).json()["edits"] == 1
    assert client.delete(url + "?all=1").json()["edits"] == 0
    assert abs(_mean(client.get(result).content) - _mean(before)) < 1
    assert client.delete(url).status_code == 409


def test_retouch_count_is_limited(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_EDITS", 1)
    _, body = _job(client)
    url = "/api/retouch/" + body["job_id"] + "/0"
    poly = {"polygon": [[1, 1], [20, 1], [20, 20]]}
    assert client.post(url, json=poly).status_code == 200
    assert client.post(url, json=poly).status_code == 409


def test_failed_render_rolls_back_the_change(client, monkeypatch):
    _, body = _job(client)
    url = body["results"][0]["url"]

    def boom(*a, **k):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(main, "_render_item", boom)
    assert client.put(url + "/adjust", json={"contrast": 40}).status_code == 500
    assert client.post("/api/retouch/" + body["job_id"] + "/0",
                       json={"polygon": [[1, 1], [20, 1], [20, 20]]}).status_code == 500
    state = client.get("/api/jobs/" + body["job_id"]).json()["results"][0]
    assert state["adjust"]["contrast"] == 0 and state["edits"] == 0


def test_save_as_converts_format_and_renames(client):
    _, body = _job(client)
    url = body["results"][0]["url"]
    r = client.get(url + "?download=1&format=webp&quality=80&name=Clio%20rouge")
    assert r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).format == "WEBP"
    assert 'filename="Clio rouge.webp"' in r.headers["content-disposition"]
    png = client.get(url + "?format=png")
    assert Image.open(io.BytesIO(png.content)).format == "PNG"
    assert "content-disposition" not in png.headers
    assert client.get(url + "?format=gif").status_code == 422
    hostile = client.get(url + "?download=1&name=..%2F..%2Fetc%2Fpasswd")
    assert "/" not in hostile.headers["content-disposition"].split("filename*")[0]


def test_zip_contains_every_result_with_unique_names(client):
    job_id, _ = _job(client, n=3)
    r = client.get("/api/jobs/" + job_id + "/zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    assert archive.testzip() is None
    names = archive.namelist()
    assert len(names) == len(set(names)) == 3
    assert Image.open(io.BytesIO(archive.read(names[0]))).format == "JPEG"


def test_zip_subset_and_conversion(client):
    job_id, _ = _job(client, n=3)
    r = client.get("/api/jobs/" + job_id + "/zip?indices=0,2&format=webp&name=lot")
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(archive.namelist()) == 2 and all(n.endswith(".webp") for n in archive.namelist())
    assert 'filename="lot.zip"' in r.headers["content-disposition"]
    assert client.get("/api/jobs/" + job_id + "/zip?indices=9").status_code == 404
    assert client.get("/api/jobs/" + job_id + "/zip?indices=a").status_code == 422
    assert client.get("/api/jobs/inconnu/zip").status_code == 404


def test_auto_adjust_suggests_without_applying(client):
    job_id, _ = _job(client, n=2)
    r = client.post("/api/jobs/" + job_id + "/auto-adjust", json={})
    assert r.status_code == 200 and set(r.json()["adjust"]) == {"0", "1"}
    only = client.post("/api/jobs/" + job_id + "/auto-adjust", json={"indices": [1]}).json()["adjust"]
    assert set(only) == {"1"} and set(only["1"]) == set(processing.ADJUST_KEYS)
    assert client.get("/api/jobs/" + job_id).json()["results"][0]["rev"] == 1


def test_results_from_older_versions_stay_editable(client):
    """Avant la v1.3 il n'y avait ni maître ni vignette : le résultat existant sert de maître."""
    job_id, body = _job(client)
    job = main.jobs.get(job_id)
    item = job.results[0]
    for name in (item.master_file, item.thumb_file):
        os.remove(os.path.join(main.jobs.job_dir(job_id), name))
    item.master_file = item.thumb_file = item.stats = None
    url = body["results"][0]["url"]
    assert client.get(url + "?thumb=1").headers["content-type"] == "image/jpeg"  # repli sur l'image entière
    assert client.put(url + "/adjust", json={"brightness": 30}).status_code == 200
    assert item.master_file and item.thumb_file
    assert client.post("/api/jobs/" + job_id + "/auto-adjust", json={}).status_code == 200


def test_replace_mode_adjust_leaves_the_background_untouched(client, monkeypatch):
    def fake_remove(image, model=None):
        rgba = Image.new("RGBA", image.size, (0, 0, 0, 0))
        rgba.paste(Image.new("RGBA", (120, 80), (150, 90, 60, 255)), (100, 80))
        return rgba

    monkeypatch.setattr(processing, "_remove_background", fake_remove)
    bg = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 160, 40)).save(bg, "PNG")
    bg_id = client.post("/api/backgrounds", files={"file": ("vert.png", bg.getvalue(), "image/png")}).json()["id"]
    try:
        job_id, body = _job(client, bg_mode="replace", background_id=bg_id, out_format="png")
        r = body["results"][0]
        assert r["fg_only"] is True and "mask_0.png" in os.listdir(main.jobs.job_dir(job_id))
        before = np.asarray(Image.open(io.BytesIO(client.get(r["url"]).content)).convert("RGB")).astype(int)
        client.put(r["url"] + "/adjust", json={"brightness": 70, "saturation": 50})
        after = np.asarray(Image.open(io.BytesIO(client.get(r["url"]).content)).convert("RGB")).astype(int)
        assert np.array_equal(after[:60], before[:60])        # fond : pixel pour pixel identique
        assert after[200, 160].sum() > before[200, 160].sum()  # véhicule (posé en bas, centré) éclairci
    finally:
        client.delete("/api/backgrounds/" + bg_id)


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers  # HTTP simple : pas de HSTS
    assert "content-security-policy" not in client.get("/api/docs").headers


def test_valid_session_is_not_blocked_by_an_ip_lockout(client, monkeypatch):
    from app import auth as auth_module

    monkeypatch.setattr(auth_module, "FAILURE_DELAY_S", 0)
    monkeypatch.setattr(main.auth, "password", "pw")
    monkeypatch.setattr(main.auth, "secret", "s")
    assert client.post("/api/auth/login", json={"password": "pw"}).status_code == 204
    intruder = TestClient(main.app)  # même IP, sans session
    for _ in range(5):
        intruder.post("/api/auth/login", json={"password": "faux"})
    try:
        assert intruder.post("/api/auth/login", json={"password": "pw"}).status_code == 429
        assert intruder.get("/api/backgrounds", headers={"Authorization": "Bearer pw"}).status_code == 429
        assert client.get("/api/backgrounds").status_code == 200  # la session ouverte continue de fonctionner
    finally:
        main.auth.limiter.reset("testclient")
