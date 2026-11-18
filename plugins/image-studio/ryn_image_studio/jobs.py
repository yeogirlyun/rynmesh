"""Image jobs: one queue on this computer, one image at a time, history per colleague.

State lives in ``<home>/image-studio/studio.db`` (SQLite, WAL) and image files
under ``<home>/image-studio/files``. A job is one image; asking for 2 or 4
creates that many jobs in one batch.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import generator, prompts, sizes, templates
from .assets import AssetError, AssetStore

MAX_ACTIVE_PER_CALLER = 20
MAX_BRIEF = 2000
MAX_WORDS = 200
MAX_REFERENCES = 5
MAX_DERIVED_SIZES = 6
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
UPLOAD_TTL_S = 24 * 3600
RETENTION_S = 90 * 24 * 3600
MAX_ATTEMPTS = 3
DEFAULT_SECONDS = 60.0
REFERENCE_MISSING = "image_reference_missing"
STATES = ("queued", "running", "succeeded", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, caller TEXT NOT NULL, caller_name TEXT NOT NULL, caller_peer TEXT NOT NULL,
  batch TEXT NOT NULL, purpose TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
  brief TEXT NOT NULL, words TEXT NOT NULL, refs TEXT NOT NULL, source_job TEXT,
  state TEXT NOT NULL, failure TEXT, failure_detail TEXT, priority INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL, seconds REAL, model TEXT,
  scene TEXT, style TEXT, copy_json TEXT NOT NULL DEFAULT '{}',
  derived_from TEXT, edit TEXT, transparent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_caller ON jobs(caller, created_at);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, priority, created_at);
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY, caller TEXT NOT NULL, created_at REAL NOT NULL
);
"""


class JobError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def caller_key(resource: str, member_id: str, peer: str) -> str:
    """History follows the person (member), not one device; falls back to the device."""
    return f"{resource}|{member_id}" if member_id else f"{resource}|peer:{peer}"


def _dir_name(caller: str) -> str:
    return hashlib.sha256(caller.encode("utf-8")).hexdigest()[:24]


def compose_brief(row: dict[str, Any]) -> str:
    return prompts.compose(row)


class JobStore:
    def __init__(self, home: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(home) / "image-studio"
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.root / "studio.db"), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
        for name, definition in (("scene", "TEXT"), ("style", "TEXT"),
                                 ("copy_json", "TEXT NOT NULL DEFAULT '{}'"), ("failure_detail", "TEXT"),
                                 ("derived_from", "TEXT"), ("edit", "TEXT"),
                                 ("transparent", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self._db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        self.assets = AssetStore(self.root / "assets", self._db, self._lock, clock)
        self.changed = threading.Condition(self._lock)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- helpers ------------------------------------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def caller_dir(self, caller: str) -> Path:
        path = self.files / _dir_name(caller)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def image_path(self, row: dict[str, Any], kind: str = "png") -> Path:
        return self.caller_dir(row["caller"]) / f"{row['id']}.{'png' if kind == 'png' else 'jpg'}"

    def upload_path(self, caller: str, upload_id: str) -> Path:
        return self.caller_dir(caller) / f"upload-{upload_id}.png"

    # ---- uploads ----------------------------------------------------------------------
    def add_upload(self, caller: str, raw: bytes) -> dict[str, Any]:
        try:
            sizes.check_upload(raw, MAX_UPLOAD_BYTES)
            png = sizes.as_png(raw)
        except sizes.SizeError as exc:
            raise JobError(str(exc), 413 if "large" in str(exc) else 400) from None
        upload_id = secrets.token_hex(12)
        self.upload_path(caller, upload_id).write_bytes(png)
        self._exec("INSERT INTO uploads(id, caller, created_at) VALUES (?,?,?)", (upload_id, caller, self.clock()))
        return {"upload_id": upload_id}

    def upload_bytes(self, caller: str, upload_id: str) -> bytes:
        row = self._one("SELECT * FROM uploads WHERE id=? AND caller=?", (upload_id, caller))
        if row is None:
            raise JobError(REFERENCE_MISSING, 404)
        return self.upload_path(caller, upload_id).read_bytes()

    # ---- submitting -------------------------------------------------------------------
    def submit(self, caller: str, caller_name: str, caller_peer: str, body: Any) -> list[dict[str, Any]]:
        if not isinstance(body, dict) or set(body) - {"purpose", "width", "height", "brief", "words", "count",
                                                       "references", "source_job", "scene", "style", "copy",
                                                       "edit", "transparent"}:
            raise JobError("image_bad_request")
        scene, style = body.get("scene"), body.get("style")
        if scene is not None and (not isinstance(scene, str) or scene not in templates.SCENES):
            raise JobError("image_bad_scene")
        if style is not None and (not isinstance(style, str) or style not in templates.STYLES):
            raise JobError("image_bad_style")
        purpose = body.get("purpose")
        if purpose is None and scene is not None:
            purpose = templates.SCENES[scene]["purpose"]
        try:
            purpose, width, height = sizes.resolve(purpose, body.get("width"), body.get("height"))
        except sizes.SizeError as exc:
            raise JobError(str(exc)) from None
        scene = scene or templates.PURPOSE_SCENES.get(purpose)
        brief, words = body.get("brief", ""), body.get("words", "")
        if not isinstance(brief, str) or len(brief) > MAX_BRIEF:
            raise JobError("image_bad_brief")
        if not isinstance(words, str) or len(words) > MAX_WORDS:
            raise JobError("image_bad_words")
        copy = body.get("copy", {})
        if not isinstance(copy, dict) or any(
            field not in templates.COPY_FIELDS or not isinstance(value, str)
            or len(value) > templates.COPY_FIELDS[field][1]
            for field, value in copy.items()
        ):
            raise JobError("image_bad_copy")
        copy_json = json.dumps({field: value.strip() for field, value in copy.items()}, ensure_ascii=False)
        count = body.get("count", 1)
        if count not in (1, 2, 4):
            raise JobError("image_bad_count")
        references = body.get("references", [])
        source = body.get("source_job")
        edit = body.get("edit")
        if "edit" in body and (not isinstance(edit, str) or edit not in prompts.EDIT_INSTRUCTIONS or source is None):
            raise JobError("image_bad_edit")
        transparent = body.get("transparent", False)
        if not isinstance(transparent, bool):
            raise JobError("image_bad_transparent")
        if not isinstance(references, list) or len(references) + (source is not None) > MAX_REFERENCES:
            raise JobError("image_bad_references")
        # A brief is optional once copy or images say what to make.
        if not (brief.strip() or words.strip() or any(copy.values()) or references or source is not None):
            raise JobError("image_bad_brief")
        refs = []
        for ref in references:
            if isinstance(ref, str):
                ref = {"id": ref, "role": "material"}
            if isinstance(ref, dict) and "asset" in ref:
                refs.append(self._asset_reference(caller, ref))
                continue
            if (not isinstance(ref, dict) or ref.keys() != {"id", "role"}
                    or not isinstance(ref["id"], str) or ref["role"] not in ("material", "style")):
                raise JobError("image_bad_references")
            self.upload_bytes(caller, ref["id"])  # must exist and belong to this caller
            refs.append(ref)
        job = {
            "purpose": purpose, "width": width, "height": height, "brief": brief.strip(), "words": words.strip(),
            "refs": json.dumps(refs), "source_job": source, "scene": scene, "style": style, "copy_json": copy_json,
            "derived_from": None, "edit": edit, "transparent": transparent,
        }
        return self._enqueue(caller, caller_name, caller_peer, [job] * count)

    def derive(self, caller: str, caller_name: str, caller_peer: str, job_id: str, body: Any) -> list[dict[str, Any]]:
        original = self.job(caller, job_id)
        if original["state"] != "succeeded":
            raise JobError("image_not_ready", 409)
        requested = body.get("sizes") if isinstance(body, dict) and set(body) == {"sizes"} else None
        if not isinstance(requested, list) or not 1 <= len(requested) <= MAX_DERIVED_SIZES:
            raise JobError("image_bad_derive_sizes")

        jobs = []
        for spec in requested:
            if not isinstance(spec, dict):
                raise JobError("image_bad_derive_sizes")
            try:
                if spec.keys() == {"purpose"} and isinstance(spec["purpose"], str):
                    purpose, width, height = sizes.resolve(spec["purpose"])
                elif spec.keys() == {"width", "height"}:
                    purpose, width, height = sizes.resolve("custom", spec["width"], spec["height"])
                else:
                    raise JobError("image_bad_derive_sizes")
            except sizes.SizeError as exc:
                raise JobError(str(exc)) from None
            jobs.append({
                "purpose": purpose, "width": width, "height": height, "brief": "", "words": "",
                "refs": "[]", "source_job": job_id, "scene": original["scene"], "style": original["style"],
                "copy_json": "{}", "derived_from": job_id, "edit": None, "transparent": original["transparent"],
            })
        return self._enqueue(caller, caller_name, caller_peer, jobs)

    def _enqueue(self, caller: str, caller_name: str, caller_peer: str,
                 jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        source = jobs[0]["source_job"]
        batch, now = secrets.token_hex(8), self.clock()
        ids = [secrets.token_hex(12) for _ in jobs]
        with self.changed:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                if source is not None:
                    src = (self._one("SELECT state FROM jobs WHERE id=? AND caller=?", (source, caller))
                           if isinstance(source, str) else None)
                    if src is None or src["state"] != "succeeded":
                        raise JobError(REFERENCE_MISSING, 404)
                active = self._one(
                    "SELECT COUNT(*) n FROM jobs WHERE caller=? AND state IN ('queued','running')", (caller,)
                )
                if active["n"] + len(jobs) > MAX_ACTIVE_PER_CALLER:
                    raise JobError("image_too_many_active", 429)
                self._db.executemany("""
                    INSERT INTO jobs(id, caller, caller_name, caller_peer, batch, purpose, width, height, brief,
                                     words, refs, source_job, scene, style, copy_json, derived_from, edit,
                                     transparent, state, created_at)
                    VALUES (:id, :caller, :caller_name, :caller_peer, :batch, :purpose, :width, :height, :brief,
                            :words, :refs, :source_job, :scene, :style, :copy_json, :derived_from, :edit,
                            :transparent, 'queued', :created_at)
                """, [{**job, "id": job_id, "caller": caller, "caller_name": caller_name[:80],
                       "caller_peer": caller_peer, "batch": batch, "created_at": now + index * 1e-6}
                      for index, (job_id, job) in enumerate(zip(ids, jobs))])
            self.changed.notify_all()
            return [self.view(self.job(caller, job_id)) for job_id in ids]

    def _asset_reference(self, caller: str, ref: dict[str, Any]) -> dict[str, str]:
        asset_id = ref["asset"]
        if set(ref) - {"asset", "role"} or not isinstance(asset_id, str):
            raise JobError("image_bad_references")
        if "role" in ref and ref["role"] not in ("material", "style"):
            raise JobError("image_bad_references")
        try:
            kind = self.assets.kind(caller, asset_id)
        except AssetError as exc:
            raise JobError(REFERENCE_MISSING, 404) from exc
        return {"asset": asset_id, "role": ref.get("role", "style" if kind == "style" else "material")}

    # ---- reading ---------------------------------------------------------------------
    def job(self, caller: str | None, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or len(job_id) != 24:
            raise JobError("image_not_found", 404)
        row = (self._one("SELECT * FROM jobs WHERE id=?", (job_id,)) if caller is None
               else self._one("SELECT * FROM jobs WHERE id=? AND caller=?", (job_id, caller)))
        if row is None:
            raise JobError("image_not_found", 404)
        return row

    def average_seconds(self) -> float:
        rows = self._rows("SELECT seconds FROM jobs WHERE state='succeeded' AND seconds IS NOT NULL "
                          "ORDER BY finished_at DESC LIMIT 20")
        return sum(r["seconds"] for r in rows) / len(rows) if rows else DEFAULT_SECONDS

    def queue(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM jobs WHERE state IN ('queued','running') "
                          "ORDER BY state='running' DESC, priority DESC, created_at ASC")

    def view(self, row: dict[str, Any], *, queue: list[dict[str, Any]] | None = None,
             average: float | None = None, owner: bool = False) -> dict[str, Any]:
        roles = prompts.reference_roles(row)
        failure = row["failure"]
        failure_text = "参考图不见了" if failure == REFERENCE_MISSING else generator.FAILURES.get(failure)
        out = {"id": row["id"], "batch": row["batch"], "purpose": row["purpose"], "width": row["width"],
               "height": row["height"], "brief": row["brief"], "words": row["words"], "state": row["state"],
               "failure": failure, "failure_text": failure_text,
               "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
               "seconds": row["seconds"], "references": len(roles), "reference_roles": roles,
               "source_job": row["source_job"], "scene": templates.job_scene(row), "style": row["style"],
               "copy": templates.job_copy(row), "derived_from": row["derived_from"],
               "edit": row["edit"], "transparent": bool(row["transparent"])}
        if row["state"] == "queued":
            queue = self.queue() if queue is None else queue
            average = self.average_seconds() if average is None else average
            ahead = next((i for i, q in enumerate(queue) if q["id"] == row["id"]), len(queue))
            running = queue and queue[0]["state"] == "running"
            out["position"] = ahead + (0 if running else 1)
            out["eta_seconds"] = round(ahead * average)
        if owner:
            out.update({"caller_name": row["caller_name"], "caller": row["caller"],
                        "priority": row["priority"], "failure_detail": row["failure_detail"]})
        return out

    def list(self, caller: str, *, state: str = "all", limit: int = 100) -> list[dict[str, Any]]:
        where = {"all": "", "active": " AND state IN ('queued','running')",
                 "done": " AND state IN ('succeeded','failed','cancelled')"}.get(state)
        if where is None:
            raise JobError("image_bad_request")
        rows = self._rows(f"SELECT * FROM jobs WHERE caller=?{where} ORDER BY created_at DESC LIMIT ?",
                          (caller, max(1, min(int(limit), 200))))
        queue, average = self.queue(), self.average_seconds()
        return [self.view(r, queue=queue, average=average) for r in rows]

    # ---- changing ----------------------------------------------------------------------
    def cancel(self, caller: str | None, job_id: str) -> dict[str, Any]:
        row = self.job(caller, job_id)
        with self.changed:
            if row["state"] == "queued":
                self._exec("UPDATE jobs SET state='cancelled', failure='cancelled', finished_at=? WHERE id=? AND state='queued'",
                           (self.clock(), job_id))
            elif row["state"] == "running":
                self._exec("UPDATE jobs SET failure='cancelled' WHERE id=? AND state='running'", (job_id,))
            self.changed.notify_all()
        return self.view(self.job(caller, job_id))

    def retry(self, caller: str, job_id: str) -> dict[str, Any]:
        row = self.job(caller, job_id)
        if row["state"] not in ("failed", "cancelled"):
            raise JobError("image_not_retryable", 409)
        with self.changed:
            self._exec("UPDATE jobs SET state='queued', failure=NULL, failure_detail=NULL, attempt=0, started_at=NULL, finished_at=NULL, "
                       "created_at=?, priority=0 WHERE id=?", (self.clock(), job_id))
            self.changed.notify_all()
        return self.view(self.job(caller, job_id))

    def prioritize(self, job_id: str) -> dict[str, Any]:
        row = self.job(None, job_id)
        if row["state"] != "queued":
            raise JobError("image_not_queued", 409)
        top = self._one("SELECT MAX(priority) p FROM jobs WHERE state='queued'")["p"] or 0
        self._exec("UPDATE jobs SET priority=? WHERE id=?", (top + 1, job_id))
        return self.view(self.job(None, job_id), owner=True)

    def cancel_requested(self, job_id: str) -> bool:
        row = self._one("SELECT failure FROM jobs WHERE id=?", (job_id,))
        return bool(row and row["failure"] == "cancelled")

    # ---- worker side ----------------------------------------------------------------------
    def claim(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._one("SELECT * FROM jobs WHERE state='queued' ORDER BY priority DESC, created_at ASC LIMIT 1")
            if row is None:
                return None
            self._exec("UPDATE jobs SET state='running', started_at=?, attempt=attempt+1 WHERE id=? AND state='queued'",
                       (self.clock(), row["id"]))
            return self.job(None, row["id"])

    def finish(self, job_id: str, *, png: bytes | None = None, failure: str | None = None,
               failure_detail: str | None = None, model: str = "") -> None:
        row = self.job(None, job_id)
        now = self.clock()
        seconds = round(now - (row["started_at"] or now), 1)
        with self.changed:
            if png is not None:
                self.image_path(row, "png").write_bytes(png)
                self.image_path(row, "jpg").write_bytes(sizes.thumbnail(png))
                self._exec("UPDATE jobs SET state='succeeded', failure=NULL, failure_detail=NULL, finished_at=?, seconds=?, model=? WHERE id=?",
                           (now, seconds, model, job_id))
            else:
                state = "cancelled" if failure == "cancelled" else "failed"
                self._exec("UPDATE jobs SET state=?, failure=?, failure_detail=?, finished_at=?, seconds=? WHERE id=?",
                           (state, failure or "generation_failed", failure_detail, now, seconds, job_id))
            self.changed.notify_all()

    def recover(self) -> None:
        """After a restart: interrupted jobs go back to the queue, up to MAX_ATTEMPTS."""
        now = self.clock()
        self._exec("UPDATE jobs SET state='failed', failure='interrupted', finished_at=? "
                   "WHERE state='running' AND attempt>=?", (now, MAX_ATTEMPTS))
        self._exec("UPDATE jobs SET state='cancelled', finished_at=? WHERE state='running' AND failure='cancelled'", (now,))
        self._exec("UPDATE jobs SET state='queued', started_at=NULL WHERE state='running'")

    def references(self, row: dict[str, Any]) -> list[bytes]:
        caller = row["caller"]
        try:
            refs = [
                self.assets.reference(caller, ref["asset"]) if "asset" in ref else self.upload_bytes(caller, ref["id"])
                for ref in prompts.read_references(row["refs"])
            ]
        except AssetError as exc:
            raise JobError(REFERENCE_MISSING, 404) from exc
        if row["source_job"]:
            source = self.job(row["caller"], row["source_job"])
            refs.insert(0, self.image_path(source, "png").read_bytes())
        return refs

    def prune(self) -> int:
        """Drop jobs and files past retention, and uploads nobody used within a day."""
        now = self.clock()
        with self._lock:
            old = self._rows("""SELECT * FROM jobs
                WHERE created_at < ? AND state NOT IN ('queued','running')
                AND id NOT IN (SELECT source_job FROM jobs
                               WHERE state IN ('queued','running') AND source_job IS NOT NULL)
                """, (now - RETENTION_S,))
            for row in old:
                for kind in ("png", "jpg"):
                    self.image_path(row, kind).unlink(missing_ok=True)
                self._exec("DELETE FROM jobs WHERE id=?", (row["id"],))
        live = {ref["id"] for r in self._rows("SELECT refs FROM jobs WHERE state IN ('queued','running')")
                for ref in prompts.read_references(r["refs"]) if "id" in ref}
        for up in self._rows("SELECT * FROM uploads WHERE created_at < ?", (now - UPLOAD_TTL_S,)):
            if up["id"] not in live:
                self.upload_path(up["caller"], up["id"]).unlink(missing_ok=True)
                self._exec("DELETE FROM uploads WHERE id=?", (up["id"],))
        return len(old)

    # ---- owner view -------------------------------------------------------------------------
    def usage(self, *, day_start: float) -> list[dict[str, Any]]:
        since = self.clock() - 30 * 24 * 3600
        rows = self._rows("""SELECT caller, MAX(caller_name) name,
              SUM(CASE WHEN state='succeeded' AND finished_at >= ? THEN 1 ELSE 0 END) today,
              SUM(CASE WHEN state='succeeded' THEN 1 ELSE 0 END) last_30_days,
              SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) failed
              FROM jobs WHERE created_at >= ? GROUP BY caller ORDER BY today DESC, last_30_days DESC""",
                          (day_start, since))
        return [{"caller": r["caller"], "name": r["name"], "today": r["today"] or 0,
                 "last_30_days": r["last_30_days"] or 0, "failed": r["failed"] or 0} for r in rows]


class Worker:
    """Runs queued jobs one at a time on this computer."""

    def __init__(self, store: JobStore, *, generate: Callable[..., tuple[bytes, dict[str, Any]]] = generator.generate
                 ) -> None:
        self.store = store
        self.generate = generate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: str | None = None

    def start(self) -> "Worker":
        self.store.recover()
        self.store.prune()
        self._thread = threading.Thread(target=self._run, name="image-studio-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self.store.changed:
            self.store.changed.notify_all()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        last_prune = time.monotonic()
        while not self._stop.is_set():
            job = self.store.claim()
            if job is None:
                with self.store.changed:
                    self.store.changed.wait(timeout=30)
                if time.monotonic() - last_prune > 3600:
                    self.store.prune()
                    last_prune = time.monotonic()
                continue
            self.run_one(job)

    def run_one(self, job: dict[str, Any]) -> None:
        self.current = job["id"]
        def cancelled():
            return self._stop.is_set() or self.store.cancel_requested(job["id"])
        try:
            options = {"transparent": True} if job["transparent"] else {}
            raw, info = self.generate(compose_brief(job), references=self.store.references(job),
                                      cancelled=cancelled, **options)
            png = sizes.fit(raw, job["width"], job["height"])
            if self.store.cancel_requested(job["id"]):
                self.store.finish(job["id"], failure="cancelled")
            else:
                self.store.finish(job["id"], png=png, model=str(info.get("model") or ""))
        except generator.GenerationError as exc:
            if self._stop.is_set() and exc.code == "cancelled" and not self.store.cancel_requested(job["id"]):
                return  # shutting down: recover() requeues it on the next start
            self.store.finish(job["id"], failure=exc.code, failure_detail=exc.detail)
        except JobError as exc:
            self.store.finish(job["id"], failure=exc.code if exc.code == REFERENCE_MISSING else "generation_failed")
        except Exception:
            self.store.finish(job["id"], failure="generation_failed")
        finally:
            self.current = None
