import json
import os

from app.store import JobStore


def _write_status(root, job_id, results):
    jd = os.path.join(root, job_id)
    os.makedirs(jd)
    with open(os.path.join(jd, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"id": job_id, "created": 1.0, "total": len(results), "done": 0,
                   "status": "processing", "opts": {}, "results": results}, f)
    return jd


def test_reload_closes_job_without_resumable_items(tmp_path):
    _write_status(str(tmp_path), "job1", [
        {"index": 0, "name": "a.jpg", "status": "pending", "error": None, "plates": 0, "out_file": None, "out_name": None},
    ])
    store = JobStore(str(tmp_path), 60)
    store.reload_from_disk()
    job = store.get("job1")
    assert job.status == "done"
    assert job.done == job.total == 1
    assert job.results[0].status == "error"


def test_reload_resumes_when_input_exists(tmp_path):
    jd = _write_status(str(tmp_path), "job2", [
        {"index": 0, "name": "a.jpg", "status": "processing", "error": None, "plates": 0, "out_file": None, "out_name": None},
    ])
    with open(os.path.join(jd, "in_0"), "wb") as f:
        f.write(b"x")
    store = JobStore(str(tmp_path), 60)
    store.reload_from_disk()
    job = store.get("job2")
    assert job.status == "processing"
    assert job.results[0].status == "pending"
    assert len(store.take_resume()) == 1


def test_reload_purges_leftover_originals_of_done_items(tmp_path):
    jd = _write_status(str(tmp_path), "job3", [
        {"index": 0, "name": "a.jpg", "status": "done", "error": None, "plates": 1, "out_file": "out_0.jpg", "out_name": "a.jpg"},
    ])
    for name in ("in_0", "out_0.jpg"):
        with open(os.path.join(jd, name), "wb") as f:
            f.write(b"x")
    JobStore(str(tmp_path), 60).reload_from_disk()
    assert not os.path.exists(os.path.join(jd, "in_0"))
    assert os.path.exists(os.path.join(jd, "out_0.jpg"))
