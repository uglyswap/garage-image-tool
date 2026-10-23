import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client():
    # Sans context manager : le lifespan (préchauffage rembg) n'est pas exécuté.
    return TestClient(main.app)


@pytest.fixture()
def protected(monkeypatch):
    monkeypatch.setattr(main.auth, "password", "pw")
    monkeypatch.setattr(main.auth, "secret", "s")


def test_health_open_mode_is_complete(client):
    body = client.get("/api/health").json()
    assert body["auth_required"] is False
    assert body["version"] == main.config.VERSION
    assert "plate_engine" in body


def test_health_minimal_when_unauthenticated(client, protected):
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "version": main.config.VERSION, "auth_required": True}


def test_health_full_with_bearer(client, protected):
    body = client.get("/api/health", headers={"Authorization": "Bearer pw"}).json()
    assert "plate_engine" in body


@pytest.mark.parametrize("path", ["/api/backgrounds", "/api/jobs/abc", "/api/docs", "/api/openapi.json"])
def test_api_requires_auth(client, protected, path):
    assert client.get(path).status_code == 401


def test_static_ui_stays_public(client, protected):
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_openapi_not_exposed_outside_api(client, protected):
    assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------- jobs
import re
import time

from tests.helpers import jpeg_bytes, noisy_jpeg_bytes


def _run_job(client, **form):
    data = {"bg_mode": "original", "out_format": "jpeg", "blur_enabled": "false"}
    data.update(form)
    res = client.post("/api/jobs", data=data, files=[("files", ("photo.jpg", jpeg_bytes(), "image/jpeg"))])
    return res


def _wait_done(client, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get("/api/jobs/" + job_id).json()
        if body["status"] == "done":
            return body
        time.sleep(0.1)
    raise AssertionError("job non terminé")


def test_fast_model_accepted_end_to_end(client):
    res = _run_job(client, rembg_model="isnet-general-use")
    assert res.status_code == 200, res.text
    body = _wait_done(client, res.json()["job_id"])
    assert body["results"][0]["status"] == "done"


def test_unknown_model_rejected(client):
    assert _run_job(client, rembg_model="evil-model").status_code == 400


def test_download_header_has_valid_rfc5987_filename(client):
    job_id = _run_job(client).json()["job_id"]
    body = _wait_done(client, job_id)
    r = client.get(body["results"][0]["url"] + "?download=1")
    assert re.search(r"filename\*=UTF-8''[^;]+$", r.headers["content-disposition"])


def test_retouch_is_serialised_and_atomic(client):
    job_id = _run_job(client).json()["job_id"]
    body = _wait_done(client, job_id)
    idx = body["results"][0]["index"]
    r = client.post("/api/retouch/" + job_id + "/" + str(idx),
                    json={"polygon": [[10, 10], [60, 10], [60, 40]], "strength": 3, "style": "white"})
    assert r.status_code == 200
    assert not any(n.endswith(".tmp") for n in __import__("os").listdir(main.jobs.job_dir(job_id)))


# ------------------------------------------------- RGPD et validation
import os


def _post_files(client, files, **form):
    data = {"bg_mode": "original", "out_format": "jpeg", "blur_enabled": "false"}
    data.update(form)
    return client.post("/api/jobs", data=data, files=files)


def _job_dirs():
    return set(os.listdir(main.config.JOBS_DIR))


def test_original_removed_after_processing(client):
    job_id = _run_job(client).json()["job_id"]
    _wait_done(client, job_id)
    names = os.listdir(main.jobs.job_dir(job_id))
    assert not any(n.startswith("in_") for n in names)
    assert any(n.startswith("out_") for n in names)


def test_fake_image_rejected_and_nothing_left_on_disk(client):
    before = _job_dirs()
    res = _post_files(client, [("files", ("x.jpg", b"ceci n'est pas une image", "image/jpeg"))])
    assert res.status_code == 415
    assert _job_dirs() == before


def test_wrong_content_type_rejected(client):
    res = _post_files(client, [("files", ("x.gif", jpeg_bytes(), "image/gif"))])
    assert res.status_code == 415


def test_file_size_limit(client, monkeypatch):
    monkeypatch.setattr(main.config, "MAX_FILE_MB", 1)
    before = _job_dirs()
    res = _post_files(client, [("files", ("big.jpg", b"\xff\xd8" + b"0" * (1_500_000), "image/jpeg"))])
    assert res.status_code == 413
    assert _job_dirs() == before


def test_total_size_limit(client, monkeypatch):
    monkeypatch.setattr(main.config, "MAX_TOTAL_MB", 1)
    blob = noisy_jpeg_bytes()
    assert 300_000 < len(blob) < 500_000  # deux fichiers passent, le troisieme depasse 1 Mo cumule
    res = _post_files(client, [("files", (n, blob, "image/jpeg")) for n in ("a.jpg", "b.jpg", "c.jpg")])
    assert res.status_code == 413
    assert "Lot trop volumineux" in res.json()["detail"]


def test_pixel_limit(client, monkeypatch):
    monkeypatch.setattr(main.config, "MAX_PIXELS", 100)
    res = _post_files(client, [("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
    assert res.status_code == 413


@pytest.mark.parametrize("form", [{"jpeg_quality": "10"}, {"jpeg_quality": "101"},
                                  {"blur_strength": "0"}, {"blur_strength": "9"}])
def test_form_bounds(client, form):
    res = _post_files(client, [("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))], **form)
    assert res.status_code == 422


def test_background_must_be_an_image(client):
    res = client.post("/api/backgrounds", files={"file": ("f.jpg", b"pas une image", "image/jpeg")}, data={"name": "x"})
    assert res.status_code == 415


def test_background_upload_ok_and_listed(client):
    res = client.post("/api/backgrounds", files={"file": ("f.jpg", jpeg_bytes(), "image/jpeg")}, data={"name": "showroom"})
    assert res.status_code == 200
    bg_id = res.json()["id"]
    assert any(b["id"] == bg_id for b in client.get("/api/backgrounds").json())
    assert client.delete("/api/backgrounds/" + bg_id).status_code == 200


@pytest.mark.parametrize("payload", [
    {"polygon": [[1, 1], [5, 5]]},
    {"polygon": [[i, i] for i in range(65)]},
    {"polygon": [[1, 1], [5, 5], [9, 1]], "strength": 9},
    {"polygon": [[1, 1], [5, 5], [9, 1]], "style": "evil"},
    {"polygon": [[1, "a"], [5, 5], [9, 1]]},
])
def test_retouch_payload_validation(client, payload):
    job_id = _run_job(client).json()["job_id"]
    body = _wait_done(client, job_id)
    r = client.post("/api/retouch/" + job_id + "/" + str(body["results"][0]["index"]), json=payload)
    assert r.status_code == 422


def test_job_status_exposes_plate_options(client):
    job_id = _run_job(client, plate_style="white").json()["job_id"]
    body = _wait_done(client, job_id)
    assert body["blur_enabled"] is False
    assert body["plate_style"] == "white"
