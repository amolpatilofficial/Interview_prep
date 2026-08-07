"""DuckDB persistence layer.

DuckDB is single-writer, so every statement goes through one connection guarded
by a re-entrant lock. All calls are short; the runner wraps them in
``asyncio.to_thread`` where they sit on a hot path.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              VARCHAR PRIMARY KEY,
    mode            VARCHAR,
    urls_total      INTEGER,
    status          VARCHAR,
    created_at      TIMESTAMP,
    finished_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applications (
    id              VARCHAR PRIMARY KEY,
    batch_id        VARCHAR,
    url             VARCHAR,
    final_url       VARCHAR,
    company         VARCHAR,
    role            VARCHAR,
    location        VARCHAR,
    ats             VARCHAR,
    mode            VARCHAR,
    status          VARCHAR,
    fields_total    INTEGER DEFAULT 0,
    fields_filled   INTEGER DEFAULT 0,
    files_uploaded  INTEGER DEFAULT 0,
    error           VARCHAR,
    screenshot      VARCHAR,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS submitted_fields (
    id                  VARCHAR PRIMARY KEY,
    application_id      VARCHAR,
    field_id            VARCHAR,
    question            VARCHAR,
    normalized_question VARCHAR,
    field_type          VARCHAR,
    action              VARCHAR,
    answer              VARCHAR,
    confidence          DOUBLE,
    source              VARCHAR,
    required            BOOLEAN,
    filled              BOOLEAN,
    error               VARCHAR,
    created_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS answer_bank (
    normalized_question VARCHAR PRIMARY KEY,
    question            VARCHAR,
    field_type          VARCHAR,
    answer              VARCHAR,
    times_used          INTEGER DEFAULT 1,
    pinned              BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMP,
    last_used_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS run_events (
    id              VARCHAR PRIMARY KEY,
    batch_id        VARCHAR,
    application_id  VARCHAR,
    level           VARCHAR,
    message         VARCHAR,
    created_at      TIMESTAMP
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._con = duckdb.connect(str(path))
        with self._lock:
            self._con.execute(SCHEMA)

    # ---------------------------------------------------------------- helpers
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._con.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._con.execute(sql, list(params))

    # ---------------------------------------------------------------- batches
    def create_batch(self, batch_id: str, mode: str, urls_total: int) -> None:
        self._exec(
            "INSERT INTO batches (id, mode, urls_total, status, created_at) VALUES (?,?,?,?,?)",
            (batch_id, mode, urls_total, "running", now()),
        )

    def finish_batch(self, batch_id: str, status: str = "done") -> None:
        self._exec(
            "UPDATE batches SET status = ?, finished_at = ? WHERE id = ?",
            (status, now(), batch_id),
        )

    # ----------------------------------------------------------- applications
    def create_application(self, app_id: str, batch_id: str, url: str, mode: str) -> None:
        ts = now()
        self._exec(
            """INSERT INTO applications (id, batch_id, url, mode, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (app_id, batch_id, url, mode, "queued", ts, ts),
        )

    def update_application(self, app_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._exec(
            f"UPDATE applications SET {assignments} WHERE id = ?",
            (*fields.values(), app_id),
        )

    def get_application(self, app_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM applications WHERE id = ?", (app_id,))
        return rows[0] if rows else None

    def list_applications(self, limit: int = 200, batch_id: str | None = None) -> list[dict]:
        if batch_id:
            return self._rows(
                "SELECT * FROM applications WHERE batch_id = ? ORDER BY created_at DESC LIMIT ?",
                (batch_id, limit),
            )
        return self._rows(
            "SELECT * FROM applications ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def stats(self) -> dict:
        rows = self._rows(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        )
        by_status = {r["status"]: int(r["n"]) for r in rows}
        total = sum(by_status.values())
        companies = self._rows(
            """SELECT COUNT(DISTINCT company) AS n FROM applications
               WHERE company IS NOT NULL AND company <> ''"""
        )
        answers = self._rows("SELECT COUNT(*) AS n FROM answer_bank")
        fields = self._rows("SELECT COUNT(*) AS n FROM submitted_fields WHERE filled")
        return {
            "total": total,
            "by_status": by_status,
            "companies": int(companies[0]["n"]) if companies else 0,
            "answers_remembered": int(answers[0]["n"]) if answers else 0,
            "fields_filled": int(fields[0]["n"]) if fields else 0,
        }

    # -------------------------------------------------------------- field log
    def record_fields(self, app_id: str, records: list[dict]) -> None:
        if not records:
            return
        ts = now()
        with self._lock:
            self._con.executemany(
                """INSERT INTO submitted_fields
                   (id, application_id, field_id, question, normalized_question, field_type,
                    action, answer, confidence, source, required, filled, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        new_id("fld"),
                        app_id,
                        r.get("field_id"),
                        r.get("question"),
                        r.get("normalized_question"),
                        r.get("field_type"),
                        r.get("action"),
                        r.get("answer"),
                        float(r.get("confidence") or 0.0),
                        r.get("source"),
                        bool(r.get("required")),
                        bool(r.get("filled")),
                        r.get("error"),
                        ts,
                    )
                    for r in records
                ],
            )

    def application_fields(self, app_id: str) -> list[dict]:
        return self._rows(
            """SELECT * FROM submitted_fields WHERE application_id = ?
               ORDER BY required DESC, created_at ASC""",
            (app_id,),
        )

    # ------------------------------------------------------------ answer bank
    def remember(self, normalized: str, question: str, field_type: str, answer: str) -> None:
        if not normalized or answer is None or answer == "":
            return
        ts = now()
        existing = self._rows(
            "SELECT normalized_question, pinned FROM answer_bank WHERE normalized_question = ?",
            (normalized,),
        )
        if existing:
            if existing[0]["pinned"]:
                # A pinned answer is user-owned; only bump usage stats.
                self._exec(
                    """UPDATE answer_bank SET times_used = times_used + 1, last_used_at = ?
                       WHERE normalized_question = ?""",
                    (ts, normalized),
                )
                return
            self._exec(
                """UPDATE answer_bank
                   SET answer = ?, question = ?, field_type = ?,
                       times_used = times_used + 1, last_used_at = ?
                   WHERE normalized_question = ?""",
                (answer, question, field_type, ts, normalized),
            )
        else:
            self._exec(
                """INSERT INTO answer_bank
                   (normalized_question, question, field_type, answer, times_used,
                    pinned, created_at, last_used_at)
                   VALUES (?,?,?,?,1,FALSE,?,?)""",
                (normalized, question, field_type, answer, ts, ts),
            )

    def lookup_answers(self, normalized_list: list[str]) -> dict[str, dict]:
        if not normalized_list:
            return {}
        placeholders = ",".join("?" for _ in normalized_list)
        rows = self._rows(
            f"SELECT * FROM answer_bank WHERE normalized_question IN ({placeholders})",
            normalized_list,
        )
        return {r["normalized_question"]: r for r in rows}

    def all_answers(self, limit: int = 500) -> list[dict]:
        return self._rows(
            """SELECT * FROM answer_bank
               ORDER BY pinned DESC, times_used DESC, last_used_at DESC LIMIT ?""",
            (limit,),
        )

    def upsert_answer(self, normalized: str, question: str, answer: str, pinned: bool = True) -> None:
        ts = now()
        exists = self._rows(
            "SELECT 1 FROM answer_bank WHERE normalized_question = ?", (normalized,)
        )
        if exists:
            self._exec(
                """UPDATE answer_bank SET answer = ?, question = ?, pinned = ?, last_used_at = ?
                   WHERE normalized_question = ?""",
                (answer, question, pinned, ts, normalized),
            )
        else:
            self._exec(
                """INSERT INTO answer_bank
                   (normalized_question, question, field_type, answer, times_used,
                    pinned, created_at, last_used_at)
                   VALUES (?,?,?,?,0,?,?,?)""",
                (normalized, question, "text", answer, pinned, ts, ts),
            )

    def delete_answer(self, normalized: str) -> None:
        self._exec("DELETE FROM answer_bank WHERE normalized_question = ?", (normalized,))

    # ----------------------------------------------------------------- events
    def log_event(self, batch_id: str | None, app_id: str | None, level: str, message: str) -> dict:
        rec = {
            "id": new_id("evt"),
            "batch_id": batch_id,
            "application_id": app_id,
            "level": level,
            "message": message,
            "created_at": now(),
        }
        self._exec(
            """INSERT INTO run_events (id, batch_id, application_id, level, message, created_at)
               VALUES (?,?,?,?,?,?)""",
            tuple(rec.values()),
        )
        return rec

    def recent_events(self, limit: int = 200) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM run_events ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return list(reversed(rows))


db = Database(settings.db_path)
