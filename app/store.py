"""Persistance légère : arrière-plans (JSON + fichiers) et jobs (mémoire + disque).

- Arrière-plans : persistés sur disque (volume Docker /data), survivent aux redémarrages.
- Jobs : en mémoire + fichiers temporaires, nettoyés automatiquement après TTL.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger(__name__)


def _safe_ext(filename: str, fallback: str = ".jpg") -> str:
    ext = os.path.splitext(filename)[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else fallback


# ----------------------------- Arrière-plans -----------------------------
@dataclass
class Background:
    id: str
    name: str
    file: str
    created: float


class BackgroundStore:
    def __init__(self, directory: str, index_file: str):
        self.dir = directory
        self.index_file = index_file
        self._lock = threading.Lock()
        self._items: dict[str, Background] = {}
        self._load()

    def _load(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        if not os.path.isfile(self.index_file):
            return
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                for raw in json.load(f):
                    item = Background(**raw)
                    if os.path.isfile(os.path.join(self.dir, item.file)):
                        self._items[item.id] = item
        except Exception as exc:  # noqa: BLE001
            logger.warning("Index des arrière-plans illisible : %s", exc)

    def _save(self) -> None:
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump([{"id": i.id, "name": i.name, "file": i.file, "created": i.created}
                       for i in self._items.values()], f, ensure_ascii=False, indent=1)

    def add(self, name: str, filename: str, data: bytes) -> Background:
        bg_id = uuid.uuid4().hex[:12]
        file = bg_id + _safe_ext(filename)
        with self._lock:
            with open(os.path.join(self.dir, file), "wb") as f:
                f.write(data)
            item = Background(bg_id, name.strip() or filename, file, time.time())
            self._items[bg_id] = item
            self._save()
        return item

    def list(self) -> list[Background]:
        return sorted(self._items.values(), key=lambda i: i.created)

    def get(self, bg_id: str) -> Background | None:
        return self._items.get(bg_id)

    def path(self, item: Background) -> str:
        return os.path.join(self.dir, item.file)

    def delete(self, bg_id: str) -> bool:
        with self._lock:
            item = self._items.pop(bg_id, None)
            if not item:
                return False
            try:
                os.remove(os.path.join(self.dir, item.file))
            except OSError:
                pass
            self._save()
        return True


# --------------------------------- Jobs ----------------------------------
@dataclass
class ResultItem:
    index: int
    name: str
    status: str = "pending"      # pending | processing | done | error
    error: str | None = None
    plates: int = 0
    out_file: str | None = None
    out_name: str | None = None


@dataclass
class Job:
    id: str
    created: float
    total: int
    done: int = 0
    status: str = "processing"   # processing | done
    results: list[ResultItem] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class JobStore:
    def __init__(self, jobs_dir: str, ttl_minutes: int):
        self.dir = jobs_dir
        self.ttl = ttl_minutes * 60
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def create(self, names: list[str]) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, time.time(), len(names),
                  results=[ResultItem(i, n) for i, n in enumerate(names)])
        with self._lock:
            self._jobs[job_id] = job
        os.makedirs(self.job_dir(job_id), exist_ok=True)
        return job

    def job_dir(self, job_id: str) -> str:
        return os.path.join(self.dir, job_id)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save_status(self, job: Job) -> None:
        with job.lock:
            data = {
                "id": job.id, "created": job.created, "total": job.total,
                "done": job.done, "status": job.status,
                "results": [{"index": r.index, "name": r.name, "status": r.status,
                             "error": r.error, "plates": r.plates,
                             "out_file": r.out_file, "out_name": r.out_name}
                            for r in job.results],
            }
        path = os.path.join(self.job_dir(job.id), "status.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)

    def reload_from_disk(self) -> int:
        """Recharge les jobs apres un redemarrage : les liens de telechargement survivent.
        Les anciens jobs (sans status.json) sont reconstruits depuis leurs fichiers."""
        if not os.path.isdir(self.dir):
            return 0
        count = 0
        for job_id in os.listdir(self.dir):
            jd = os.path.join(self.dir, job_id)
            sj = os.path.join(jd, "status.json")
            if os.path.isfile(sj):
                try:
                    with open(sj, encoding="utf-8") as f:
                        d = json.load(f)
                    job = Job(d["id"], d["created"], d["total"], done=d["done"],
                              status=d["status"],
                              results=[ResultItem(**r) for r in d["results"]])
                    with self._lock:
                        self._jobs[job.id] = job
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Rechargement job %s impossible : %s", job_id, exc)
                continue
            outs = sorted(glob.glob(os.path.join(jd, "out_*")))
            if not outs:
                continue
            results = []
            for op in outs:
                b = os.path.basename(op)
                try:
                    idx = int(b.split("_")[1].split(".")[0])
                except ValueError:
                    continue
                results.append(ResultItem(index=idx, name="image-" + str(idx),
                                          status="done", out_file=b, out_name=b))
            if not results:
                continue
            results.sort(key=lambda r: r.index)
            job = Job(job_id, os.path.getmtime(jd), len(results),
                      done=len(results), status="done", results=results)
            with self._lock:
                self._jobs[job.id] = job
            count += 1
        if count:
            logger.info("Jobs recharges depuis le disque : %d", count)
        return count

    def cleanup_loop(self) -> None:
        """Supprime les jobs (et leurs fichiers) après expiration du TTL."""
        while True:
            time.sleep(900)
            with self._lock:
                now = time.time()
                expired = [j.id for j in self._jobs.values() if now - j.created > self.ttl]
                for job_id in expired:
                    self._jobs.pop(job_id, None)
                    shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
                if expired:
                    logger.info("Nettoyage : %d job(s) expiré(s)", len(expired))
