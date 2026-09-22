from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from server.config import Settings
from server.contracts.models import VideoMetadata, VideoSource
from server.errors import IngestError


def new_id(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(16)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """Single-process SQLite store. Every public lookup checks the verified principal."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, identity TEXT NOT NULL,
                source TEXT NOT NULL, metadata TEXT NOT NULL, revision TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                download_bytes INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS assets_owner ON assets(owner, identity);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id),
                kind TEXT NOT NULL, relative_path TEXT NOT NULL, mime TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, details TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS artifacts_asset ON artifacts(asset_id, kind);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id), owner TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                errors TEXT NOT NULL DEFAULT '[]', completed TEXT NOT NULL DEFAULT '[]');
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
            CREATE INDEX IF NOT EXISTS jobs_idempotency ON jobs(owner, idempotency_key);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (
                asset_id TEXT NOT NULL REFERENCES assets(id), metric TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset_id, metric));
        """)
        with self.transaction():
            row = self.db.execute("SELECT value FROM settings WHERE key='cursor_secret'").fetchone()
            if row:
                self.cursor_secret = bytes.fromhex(row[0])
            else:
                self.cursor_secret = secrets.token_bytes(32)
                self.db.execute("INSERT INTO settings VALUES ('cursor_secret', ?)",
                                (self.cursor_secret.hex(),))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def close(self):
        with self.lock:
            self.db.close()

    def _asset(self, owner: str, asset_id: str, include_deleted: bool = False) -> dict:
        row = self.db.execute("SELECT * FROM assets WHERE id=? AND owner=?", (asset_id, owner)).fetchone()
        if row is None or (row["deleted"] and not include_deleted):
            raise IngestError("TENANT_ACCESS_DENIED", "Asset is unavailable to this principal.",
                              next_action="resolve_video")
        if row["expires"] <= time.time() and not include_deleted:
            raise IngestError("ARTIFACT_EXPIRED", "Asset retention has expired.", next_action="resolve_video")
        result = dict(row)
        result["source"], result["metadata"] = json.loads(result["source"]), json.loads(result["metadata"])
        return result

    def asset(self, owner: str, asset_id: str, include_deleted: bool = False) -> dict:
        with self.lock:
            return self._asset(owner, asset_id, include_deleted)

    def find_asset(self, owner: str, identity: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT id FROM assets WHERE owner=? AND identity=? AND deleted=0 "
                                  "AND expires>? ORDER BY created DESC LIMIT 1",
                                  (owner, identity, time.time())).fetchone()
            return self._asset(owner, row[0]) if row else None

    def create_asset(self, owner: str, source: VideoSource, metadata: VideoMetadata) -> dict:
        with self.transaction():
            row = self.db.execute("SELECT id FROM assets WHERE owner=? AND identity=? AND deleted=0 "
                                  "AND expires>? LIMIT 1", (owner, source.identity, time.time())).fetchone()
            if row:
                return self._asset(owner, row[0])
            count = self.db.execute("SELECT count(*) FROM assets WHERE owner=? AND deleted=0 AND expires>?",
                                    (owner, time.time())).fetchone()[0]
            if count >= self.settings.max_assets:
                raise IngestError("LIMIT_EXCEEDED", "Asset limit reached; delete unused assets.",
                                  next_action="delete_asset")
            aid, now = new_id("vid"), time.time()
            self.db.execute("INSERT INTO assets(id,owner,identity,source,metadata,revision,created,expires) "
                            "VALUES (?,?,?,?,?,'r1',?,?)", (aid, owner, source.identity,
                            encode(source.model_dump()), encode(metadata.model_dump()), now,
                            now + self.settings.ttl_seconds))
            return self._asset(owner, aid)

    def check_asset_capacity(self, owner: str):
        with self.lock:
            count = self.db.execute("SELECT count(*) FROM assets WHERE owner=? AND deleted=0 AND expires>?",
                                    (owner, time.time())).fetchone()[0]
        if count >= self.settings.max_assets:
            raise IngestError("LIMIT_EXCEEDED", "Asset limit reached; delete unused assets.", next_action="delete_asset")

    def record_usage(self, owner: str, asset_id: str, metric: str, amount: int, limit: int | None = None):
        if amount < 0:
            raise ValueError("Usage must be nonnegative")
        with self.transaction():
            self._asset(owner, asset_id, include_deleted=True)
            row = self.db.execute("SELECT amount FROM usage WHERE asset_id=? AND metric=?", (asset_id, metric)).fetchone()
            total = (row[0] if row else 0) + amount
            if limit is not None and total > limit:
                raise IngestError("BUDGET_EXCEEDED", "Processing budget exceeded.", next_action="use_available_evidence")
            self.db.execute("INSERT INTO usage VALUES (?,?,?) ON CONFLICT(asset_id,metric) "
                            "DO UPDATE SET amount=excluded.amount", (asset_id, metric, total))

    def usage(self, owner: str, asset_id: str) -> dict:
        with self.lock:
            asset = self._asset(owner, asset_id, include_deleted=True)
            rows = self.db.execute("SELECT metric,amount FROM usage WHERE asset_id=?", (asset_id,)).fetchall()
            return {"download_bytes": asset["download_bytes"], **{row[0]: row[1] for row in rows}}

    def prune_temporary_files(self):
        """Run only at startup under the process lock, before starting a worker."""
        with self.lock:
            kept = {str(row[0]) for row in self.db.execute("SELECT relative_path FROM artifacts")}
            root = self.root / "assets"
            for path in root.rglob("*"):
                if path.is_file() and str(path.relative_to(self.root)) not in kept:
                    path.unlink(missing_ok=True)

    def asset_dir(self, asset_id: str) -> Path:
        # IDs come from our database, never paths supplied by tool callers.
        if len(asset_id) != 36 or not asset_id.startswith("vid_") or \
                any(c not in "0123456789abcdef" for c in asset_id[4:]):
            raise IngestError("TENANT_ACCESS_DENIED", "Invalid asset reference.")
        path = (self.root / "assets" / asset_id).resolve()
        if not path.is_relative_to(self.root):
            raise IngestError("TENANT_ACCESS_DENIED", "Invalid storage reference.")
        return path

    def artifacts(self, owner: str, asset_id: str, kind: str | None = None) -> list[dict]:
        with self.lock:
            asset = self._asset(owner, asset_id)
            sql, params = "SELECT * FROM artifacts WHERE asset_id=?", [asset_id]
            if kind is not None:
                sql += " AND kind=?"
                params.append(kind)
            rows = self.db.execute(sql + " ORDER BY created, id", params).fetchall()
            return [{**dict(r), "details": json.loads(r["details"]), "expires_at": asset["expires"]} for r in rows]

    def artifact(self, owner: str, artifact_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if row is None:
                raise IngestError("TENANT_ACCESS_DENIED", "Artifact is unavailable to this principal.")
            asset = self._asset(owner, row["asset_id"])
            return {**dict(row), "details": json.loads(row["details"]), "expires_at": asset["expires"]}

    def reconcile_artifacts(self, owner: str, asset_id: str):
        """Invalidate a completed-job cache when its retained bytes have disappeared."""
        artifacts = self.artifacts(owner, asset_id)
        missing = set()
        for artifact in artifacts:
            try:
                self.artifact_path(owner, artifact["id"])
            except IngestError as error:
                if error.code != "ARTIFACT_EXPIRED":
                    raise
                missing.add(artifact["id"])
        for artifact in artifacts:
            if artifact["kind"] == "visual_index" and missing.intersection(artifact["details"].get("frame_artifact_ids", [])):
                missing.add(artifact["id"])
        if missing:
            with self.transaction():
                self._asset(owner, asset_id)
                self.db.executemany("DELETE FROM artifacts WHERE id=?", [(aid,) for aid in missing])
                # Preserve historical task success while preventing stale cache reuse.
                self.db.execute("UPDATE jobs SET idempotency_key=idempotency_key || ':invalidated' "
                                "WHERE asset_id=? AND status='succeeded'", (asset_id,))

    def artifact_path(self, owner: str, artifact_id: str) -> Path:
        art = self.artifact(owner, artifact_id)
        path = (self.root / art["relative_path"]).resolve()
        if not path.is_relative_to(self.asset_dir(art["asset_id"])) or not path.is_file():
            raise IngestError("ARTIFACT_EXPIRED", "Artifact bytes are no longer available.",
                              next_action="prepare_video")
        return path

    def add_artifact(self, owner: str, asset_id: str, kind: str, path: Path, mime: str,
                     details: dict | None = None, artifact_id: str | None = None) -> dict:
        path = path.resolve()
        if not path.is_relative_to(self.asset_dir(asset_id)) or not path.is_file() or path.is_symlink():
            raise IngestError("UNSAFE_PATH", "Provider produced an invalid artifact path.")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        size, aid = path.stat().st_size, artifact_id or new_id("art")
        self.check_disk()
        with self.transaction():
            self._asset(owner, asset_id)
            self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", (aid, asset_id, kind,
                            str(path.relative_to(self.root)), mime, size, digest.hexdigest(),
                            encode(details or {}), time.time()))
        return self.artifact(owner, aid)

    def check_disk(self):
        size = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.is_symlink())
        if size > self.settings.max_disk_bytes:
            raise IngestError("BUDGET_EXCEEDED", "Private storage quota exceeded.",
                              next_action="delete_asset")

    def charge_bytes(self, owner: str, asset_id: str, delta: int):
        if delta < 0:
            raise ValueError("byte charges must be nonnegative")
        with self.transaction():
            # An in-flight read spent these bytes even if revocation happened concurrently.
            asset = self._asset(owner, asset_id, include_deleted=True)
            total = asset["download_bytes"] + delta
            self.db.execute("UPDATE assets SET download_bytes=? WHERE id=?", (total, asset_id))
        # Persist spent bytes even when the limit has now been reached.
        if total > self.settings.max_download_bytes:
            raise IngestError("BUDGET_EXCEEDED", "Cumulative media transfer budget exceeded.",
                              stage="downloading", next_action="use_available_evidence")
        if asset["deleted"] or asset["expires"] <= time.time():
            raise IngestError("CANCELLED", "Asset access expired or was revoked during transfer.",
                              stage="downloading", next_action="use_available_evidence")
        self.check_disk()

    def enqueue(self, owner: str, asset_id: str, key: str, request: dict) -> tuple[dict, bool]:
        with self.transaction():
            self._asset(owner, asset_id)
            row = self.db.execute("SELECT * FROM jobs WHERE owner=? AND idempotency_key=? "
                                  "AND status IN ('queued','running','cancel_requested','succeeded') "
                                  "ORDER BY created DESC LIMIT 1", (owner, key)).fetchone()
            if row:
                return self._decode_job(row), True
            count = self.db.execute("SELECT count(*) FROM jobs WHERE owner=? AND status IN "
                                    "('queued','running','cancel_requested')", (owner,)).fetchone()[0]
            if count >= self.settings.max_queued_jobs:
                raise IngestError("LIMIT_EXCEEDED", "Task queue is full.", next_action="get_job")
            jid, now = new_id("job"), time.time()
            self.db.execute("INSERT INTO jobs(id,asset_id,owner,idempotency_key,request,status,stage,created,updated) "
                            "VALUES (?,?,?,?,?,'queued','resolving',?,?)",
                            (jid, asset_id, owner, key, encode(request), now, now))
            return self._decode_job(self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), False

    @staticmethod
    def _decode_job(row) -> dict:
        result = dict(row)
        for key in ("request", "errors", "completed"):
            result[key] = json.loads(result[key])
        return result

    def job(self, owner: str, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
            if row is None:
                raise IngestError("TENANT_ACCESS_DENIED", "Job is unavailable to this principal.")
            return self._decode_job(row)

    def update_job(self, job_id: str, **updates):
        if set(updates) - {"status", "stage", "errors", "completed"}:
            raise ValueError("Invalid job update")
        for key in ("errors", "completed"):
            if key in updates:
                updates[key] = encode(updates[key])
        updates["updated"] = time.time()
        with self.transaction():
            self.db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in updates) + " WHERE id=?",
                            (*updates.values(), job_id))

    def recover(self):
        error = IngestError("WORKER_INTERRUPTED", "Processing stopped before completion; retained evidence is readable.",
                            retryable=True, next_action="prepare_video").as_dict()
        with self.transaction():
            self.db.execute("UPDATE jobs SET status='failed',errors=?,updated=? WHERE status='running'",
                            (encode([error]), time.time()))
            self.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE status='cancel_requested'",
                            (time.time(),))

    def claim_next(self) -> dict | None:
        with self.transaction():
            row = self.db.execute("SELECT j.* FROM jobs j JOIN assets a ON a.id=j.asset_id "
                                  "WHERE j.status='queued' AND a.deleted=0 AND a.expires>? "
                                  "ORDER BY j.created LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return {**self._decode_job(row), "status": "running"}

    def cancel(self, owner: str, job_id: str) -> dict:
        with self.transaction():
            job = self.job(owner, job_id)
            status = {"queued": "cancelled", "running": "cancel_requested"}.get(job["status"], job["status"])
            self.db.execute("UPDATE jobs SET status=?,updated=? WHERE id=?", (status, time.time(), job_id))
        return self.job(owner, job_id)

    def revoke(self, owner: str, asset_id: str):
        with self.transaction():
            self._asset(owner, asset_id, include_deleted=True)
            self.db.execute("UPDATE assets SET deleted=1 WHERE id=?", (asset_id,))
            self.db.execute("UPDATE jobs SET status=CASE WHEN status='queued' THEN 'cancelled' "
                            "ELSE 'cancel_requested' END,updated=? WHERE asset_id=? "
                            "AND status IN ('queued','running')", (time.time(), asset_id))

    def purge_revoked(self):
        with self.transaction():
            self.db.execute("UPDATE assets SET deleted=1 WHERE expires<=?", (time.time(),))
            self.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE status='queued' "
                            "AND asset_id IN (SELECT id FROM assets WHERE deleted=1)", (time.time(),))
            rows = self.db.execute("SELECT id FROM assets WHERE deleted=1 AND id NOT IN "
                                  "(SELECT asset_id FROM jobs WHERE status IN ('running','cancel_requested'))").fetchall()
            for row in rows:
                directory = self.asset_dir(row[0])
                if directory.exists():
                    shutil.rmtree(directory)
                self.db.execute("DELETE FROM artifacts WHERE asset_id=?", (row[0],))
                # Keep IDs/status/usage for recovery, remove external source metadata.
                self.db.execute("UPDATE assets SET metadata='{}',source='{}',identity='' WHERE id=?", (row[0],))
