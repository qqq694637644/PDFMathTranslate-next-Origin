from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from threading import Event
from typing import Any

from pdf2zh_next.const import DEFAULT_CONFIG_DIR

SCHEMA_VERSION = 1
DEFAULT_QUEUE_DB = DEFAULT_CONFIG_DIR / "gptaction-queue.sqlite3"
DEFAULT_MAX_SERIALIZED_RESPONSE_CHARS = 30000
VALID_MODES = {"LLM_BATCH", "SIMPLE_TEXT"}
RUN_STATUSES = {"ACTIVE", "COMPLETED", "FAILED", "CANCELED"}
REQUEST_STATUSES = {"PENDING", "CLAIMED", "COMPLETED", "CANCELED"}


class GPTActionQueueError(RuntimeError):
    """Base error for the durable GPT Actions queue."""


class ActiveRunExistsError(GPTActionQueueError):
    """Raised when a second active translation run is requested."""


class QueueItemTooLargeError(GPTActionQueueError):
    """Raised when one queue item cannot fit in an Action response."""

    def __init__(
        self,
        *,
        request_id: str,
        required_chars: int,
        max_chars: int,
    ):
        self.request_id = request_id
        self.required_chars = required_chars
        self.max_chars = max_chars
        super().__init__(
            "GPT Action request cannot fit in one serialized response: "
            f"request_id={request_id}, required_chars={required_chars}, "
            f"max_chars={max_chars}. Increase "
            "GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS and restart the run."
        )


class QueueRequestCanceledError(GPTActionQueueError):
    """Raised when a translator waits on a canceled request."""


class QueueWaitCanceledError(GPTActionQueueError):
    """Raised when the application cancellation event is set."""


@dataclass(frozen=True)
class EnqueueResult:
    request_id: str | None
    output_text: str | None
    reused_completed: bool


@dataclass(frozen=True)
class SubmissionResult:
    request_id: str
    status: str
    error: str | None = None


def resolve_queue_db_path(value: str | Path | None = None) -> Path:
    """Return one canonical absolute queue path shared by all processes."""
    configured = value or os.getenv("GPT_ACTION_QUEUE_DB") or DEFAULT_QUEUE_DB
    path = Path(configured).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def canonical_fingerprint(
    *,
    protocol_version: str,
    mode: str,
    lang_in: str,
    lang_out: str,
    input_text: str,
    semantic_context: dict[str, Any] | None = None,
) -> str:
    """Hash only translation-semantic inputs, never runtime scheduling fields."""
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported GPT Action request mode: {mode}")
    payload = {
        "protocol_version": str(protocol_version),
        "mode": mode,
        "lang_in": lang_in,
        "lang_out": lang_out,
        "input_text": input_text,
        "semantic_context": semantic_context or {},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat(timespec="microseconds")


def _serialized_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _action_request_item(
    *,
    request_id: str,
    claim_token: str,
    mode: str,
    lang_in: str,
    lang_out: str,
    input_text: str,
) -> dict[str, str]:
    return {
        "request_id": request_id,
        "claim_token": claim_token,
        "mode": mode,
        "lang_in": lang_in,
        "lang_out": lang_out,
        "input": input_text,
    }


def _validate_action_request_size(
    *,
    run_id: str,
    item: dict[str, str],
    max_serialized_response_chars: int,
) -> None:
    required_chars = _serialized_chars({"run_id": run_id, "requests": [item]})
    if required_chars > max_serialized_response_chars:
        raise QueueItemTooLargeError(
            request_id=item["request_id"],
            required_chars=required_chars,
            max_chars=max_serialized_response_chars,
        )


class GPTActionQueue:
    """Small SQLite queue for one personal GPT Actions translation run."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = resolve_queue_db_path(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS translation_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('ACTIVE', 'COMPLETED', 'FAILED', 'CANCELED')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_runs_active
                ON translation_runs(status)
                WHERE status = 'ACTIVE';

                CREATE TABLE IF NOT EXISTS translation_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES translation_runs(run_id),
                    protocol_version TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('LLM_BATCH', 'SIMPLE_TEXT')),
                    fingerprint TEXT NOT NULL,
                    lang_in TEXT NOT NULL,
                    lang_out TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'CLAIMED', 'COMPLETED', 'CANCELED')
                    ),
                    claim_token TEXT,
                    claimed_until TEXT,
                    output_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_translation_requests_run_status
                ON translation_requests(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS ix_translation_requests_fingerprint
                ON translation_requests(fingerprint, status, completed_at);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_translation_requests_open_fingerprint
                ON translation_requests(fingerprint)
                WHERE status IN ('PENDING', 'CLAIMED');
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_info(singleton, version)
                VALUES (1, ?)
                """,
                (SCHEMA_VERSION,),
            )
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
            if row is None or row["version"] != SCHEMA_VERSION:
                raise GPTActionQueueError(
                    "Unsupported GPT Action queue schema version: "
                    f"{None if row is None else row['version']} "
                    f"(expected {SCHEMA_VERSION})"
                )

    def verify_database(self) -> None:
        if not self.database_path.exists():
            raise GPTActionQueueError(
                f"GPT Action queue database does not exist: {self.database_path}"
            )
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def verify_schema(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
        if row is None or row["version"] != SCHEMA_VERSION:
            raise GPTActionQueueError("GPT Action queue schema is invalid")

    def verify_writable(self) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE schema_info SET version = version WHERE singleton = 1"
            )

    def start_run(self) -> str:
        now = _to_iso()
        run_id = f"run_{secrets.token_hex(12)}"
        with self._write_transaction() as connection:
            active = connection.execute(
                "SELECT run_id FROM translation_runs WHERE status = 'ACTIVE'"
            ).fetchone()
            if active is not None:
                raise ActiveRunExistsError(
                    "A GPT Action translation run is already active: "
                    f"{active['run_id']}"
                )
            connection.execute(
                """
                INSERT INTO translation_runs(
                    run_id, status, created_at, updated_at, completed_at
                ) VALUES (?, 'ACTIVE', ?, ?, NULL)
                """,
                (run_id, now, now),
            )
        return run_id

    def get_active_run_id(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM translation_runs WHERE status = 'ACTIVE'"
            ).fetchone()
        return None if row is None else str(row["run_id"])

    def get_active_run_summary(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT run_id, created_at, updated_at
                FROM translation_runs
                WHERE status = 'ACTIVE'
                """
            ).fetchone()
            if run is None:
                return None
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM translation_requests
                    WHERE run_id = ?
                    GROUP BY status
                    """,
                    (run["run_id"],),
                ).fetchall()
            }
        return {
            "run_id": str(run["run_id"]),
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
            "pending": counts.get("PENDING", 0),
            "claimed": counts.get("CLAIMED", 0),
            "completed": counts.get("COMPLETED", 0),
        }

    def recover_active_run(self, run_id: str) -> dict[str, int]:
        """Explicitly fail one orphaned active run and release its open claims."""
        now = _to_iso()
        with self._write_transaction() as connection:
            run = connection.execute(
                "SELECT status FROM translation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise GPTActionQueueError(f"GPT Action run does not exist: {run_id}")
            if run["status"] != "ACTIVE":
                raise GPTActionQueueError(
                    f"GPT Action run is not active: {run_id} ({run['status']})"
                )
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM translation_requests
                    WHERE run_id = ?
                    GROUP BY status
                    """,
                    (run_id,),
                ).fetchall()
            }
            connection.execute(
                """
                UPDATE translation_runs
                SET status = 'FAILED', updated_at = ?, completed_at = ?
                WHERE run_id = ? AND status = 'ACTIVE'
                """,
                (now, now, run_id),
            )
            connection.execute(
                """
                UPDATE translation_requests
                SET status = 'PENDING', claimed_until = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'CLAIMED'
                """,
                (now, run_id),
            )
        return {
            "pending": counts.get("PENDING", 0),
            "released_claimed": counts.get("CLAIMED", 0),
            "completed": counts.get("COMPLETED", 0),
        }

    def assert_active_run(self, run_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM translation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or row["status"] != "ACTIVE":
            raise GPTActionQueueError(f"GPT Action run is not active: {run_id}")

    def complete_run(self, run_id: str) -> None:
        now = _to_iso()
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE translation_runs
                SET status = 'COMPLETED', updated_at = ?, completed_at = ?
                WHERE run_id = ? AND status = 'ACTIVE'
                """,
                (now, now, run_id),
            )

    def fail_run(self, run_id: str) -> None:
        now = _to_iso()
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE translation_runs
                SET status = 'FAILED', updated_at = ?, completed_at = ?
                WHERE run_id = ? AND status = 'ACTIVE'
                """,
                (now, now, run_id),
            )
            connection.execute(
                """
                UPDATE translation_requests
                SET status = 'PENDING', claimed_until = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'CLAIMED'
                """,
                (now, run_id),
            )

    def cancel_run(self, run_id: str) -> None:
        now = _to_iso()
        with self._write_transaction() as connection:
            connection.execute(
                """
                UPDATE translation_runs
                SET status = 'CANCELED', updated_at = ?, completed_at = ?
                WHERE run_id = ? AND status = 'ACTIVE'
                """,
                (now, now, run_id),
            )
            connection.execute(
                """
                UPDATE translation_requests
                SET status = 'CANCELED', claimed_until = NULL,
                    updated_at = ?, completed_at = ?
                WHERE run_id = ? AND status IN ('PENDING', 'CLAIMED')
                """,
                (now, now, run_id),
            )

    def enqueue(
        self,
        *,
        run_id: str,
        protocol_version: str,
        mode: str,
        lang_in: str,
        lang_out: str,
        input_text: str,
        semantic_context: dict[str, Any] | None = None,
        reuse_completed: bool = True,
        max_serialized_response_chars: int = DEFAULT_MAX_SERIALIZED_RESPONSE_CHARS,
    ) -> EnqueueResult:
        if not input_text:
            raise ValueError("GPT Action translation input cannot be empty")
        fingerprint = canonical_fingerprint(
            protocol_version=protocol_version,
            mode=mode,
            lang_in=lang_in,
            lang_out=lang_out,
            input_text=input_text,
            semantic_context=semantic_context,
        )
        now = _to_iso()
        with self._write_transaction() as connection:
            run = connection.execute(
                "SELECT status FROM translation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "ACTIVE":
                raise GPTActionQueueError(f"GPT Action run is not active: {run_id}")

            if reuse_completed:
                completed = connection.execute(
                    """
                    SELECT output_text
                    FROM translation_requests
                    WHERE fingerprint = ? AND status = 'COMPLETED'
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (fingerprint,),
                ).fetchone()
                if completed is not None:
                    return EnqueueResult(
                        request_id=None,
                        output_text=str(completed["output_text"]),
                        reused_completed=True,
                    )

            existing = connection.execute(
                """
                SELECT request_id, run_id, status, claim_token, mode,
                       lang_in, lang_out, input_text
                FROM translation_requests
                WHERE fingerprint = ? AND status IN ('PENDING', 'CLAIMED')
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                existing_run_id = str(existing["run_id"])
                claim_token = str(existing["claim_token"] or secrets.token_urlsafe(24))
                item = _action_request_item(
                    request_id=str(existing["request_id"]),
                    claim_token=claim_token,
                    mode=str(existing["mode"]),
                    lang_in=str(existing["lang_in"]),
                    lang_out=str(existing["lang_out"]),
                    input_text=str(existing["input_text"]),
                )
                _validate_action_request_size(
                    run_id=run_id,
                    item=item,
                    max_serialized_response_chars=max_serialized_response_chars,
                )
                if existing_run_id != run_id:
                    old_run = connection.execute(
                        "SELECT status FROM translation_runs WHERE run_id = ?",
                        (existing_run_id,),
                    ).fetchone()
                    if old_run is not None and old_run["status"] == "ACTIVE":
                        raise ActiveRunExistsError(
                            "An open request belongs to another active run"
                        )
                    connection.execute(
                        """
                        UPDATE translation_requests
                        SET run_id = ?, claim_token = ?, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (run_id, claim_token, now, existing["request_id"]),
                    )
                elif existing["claim_token"] is None:
                    connection.execute(
                        """
                        UPDATE translation_requests
                        SET claim_token = ?, updated_at = ?
                        WHERE request_id = ?
                        """,
                        (claim_token, now, existing["request_id"]),
                    )
                return EnqueueResult(
                    request_id=str(existing["request_id"]),
                    output_text=None,
                    reused_completed=False,
                )

            request_id = f"req_{secrets.token_hex(16)}"
            claim_token = secrets.token_urlsafe(24)
            item = _action_request_item(
                request_id=request_id,
                claim_token=claim_token,
                mode=mode,
                lang_in=lang_in,
                lang_out=lang_out,
                input_text=input_text,
            )
            _validate_action_request_size(
                run_id=run_id,
                item=item,
                max_serialized_response_chars=max_serialized_response_chars,
            )
            connection.execute(
                """
                INSERT INTO translation_requests(
                    request_id, run_id, protocol_version, mode, fingerprint,
                    lang_in, lang_out, input_text, status, claim_token,
                    claimed_until, output_text, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, ?, ?, NULL)
                """,
                (
                    request_id,
                    run_id,
                    str(protocol_version),
                    mode,
                    fingerprint,
                    lang_in,
                    lang_out,
                    input_text,
                    claim_token,
                    now,
                    now,
                ),
            )
            return EnqueueResult(
                request_id=request_id,
                output_text=None,
                reused_completed=False,
            )

    def wait_for_result(
        self,
        request_id: str,
        *,
        cancel_event: Event | None = None,
        poll_interval_seconds: float = 0.5,
        run_id: str | None = None,
    ) -> str:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                if run_id:
                    self.cancel_run(run_id)
                raise QueueWaitCanceledError("GPT Action translation was canceled")

            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT request.status, request.output_text,
                           run.status AS run_status
                    FROM translation_requests AS request
                    JOIN translation_runs AS run ON run.run_id = request.run_id
                    WHERE request.request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
            if row is None:
                raise GPTActionQueueError(
                    f"GPT Action request does not exist: {request_id}"
                )
            if row["status"] == "COMPLETED":
                return str(row["output_text"])
            if row["status"] == "CANCELED":
                raise QueueRequestCanceledError(
                    f"GPT Action request was canceled: {request_id}"
                )
            if row["run_status"] != "ACTIVE":
                raise GPTActionQueueError(
                    "GPT Action translation run is no longer active: "
                    f"{row['run_status']}"
                )

            if cancel_event is not None:
                cancel_event.wait(poll_interval_seconds)
            else:
                time.sleep(poll_interval_seconds)

    def claim_batch(
        self,
        *,
        max_requests: int,
        max_serialized_response_chars: int,
        claim_ttl_seconds: int,
    ) -> dict[str, Any]:
        max_requests = max(1, max_requests)
        now_dt = _utcnow()
        now = _to_iso(now_dt)
        claimed_until = _to_iso(now_dt + timedelta(seconds=claim_ttl_seconds))

        with self._write_transaction() as connection:
            active = connection.execute(
                "SELECT run_id FROM translation_runs WHERE status = 'ACTIVE'"
            ).fetchone()
            if active is None:
                return {"run_id": None, "requests": []}
            run_id = str(active["run_id"])

            connection.execute(
                """
                UPDATE translation_requests
                SET status = 'PENDING', claimed_until = NULL, updated_at = ?
                WHERE run_id = ? AND status = 'CLAIMED'
                  AND claimed_until IS NOT NULL AND claimed_until <= ?
                """,
                (now, run_id, now),
            )

            candidates = connection.execute(
                """
                SELECT request_id, mode, lang_in, lang_out, input_text, claim_token
                FROM translation_requests
                WHERE run_id = ? AND status = 'PENDING'
                ORDER BY created_at, request_id
                LIMIT ?
                """,
                (run_id, max_requests),
            ).fetchall()

            selected: list[dict[str, str]] = []
            selected_tokens: list[tuple[str, str]] = []
            for row in candidates:
                token = str(row["claim_token"] or secrets.token_urlsafe(24))
                item = _action_request_item(
                    request_id=str(row["request_id"]),
                    claim_token=token,
                    mode=str(row["mode"]),
                    lang_in=str(row["lang_in"]),
                    lang_out=str(row["lang_out"]),
                    input_text=str(row["input_text"]),
                )
                tentative = {"run_id": run_id, "requests": [*selected, item]}
                required_chars = _serialized_chars(tentative)
                if required_chars > max_serialized_response_chars:
                    if not selected:
                        raise QueueItemTooLargeError(
                            request_id=str(row["request_id"]),
                            required_chars=required_chars,
                            max_chars=max_serialized_response_chars,
                        )
                    break
                selected.append(item)
                selected_tokens.append((token, str(row["request_id"])))

            for token, request_id in selected_tokens:
                connection.execute(
                    """
                    UPDATE translation_requests
                    SET status = 'CLAIMED', claim_token = ?, claimed_until = ?,
                        updated_at = ?
                    WHERE request_id = ? AND status = 'PENDING'
                    """,
                    (token, claimed_until, now, request_id),
                )

            return {"run_id": run_id, "requests": selected}

    def submit_result(
        self,
        *,
        request_id: str,
        claim_token: str,
        output_text: str,
    ) -> SubmissionResult:
        if not output_text or not output_text.strip():
            return SubmissionResult(
                request_id=request_id,
                status="ERROR",
                error="output must be a non-empty string",
            )
        now = _to_iso()
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT status, claim_token, output_text
                FROM translation_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return SubmissionResult(
                    request_id=request_id,
                    status="ERROR",
                    error="request_id was not found",
                )
            stored_token = row["claim_token"]
            if not stored_token or not secrets.compare_digest(
                str(stored_token), claim_token
            ):
                return SubmissionResult(
                    request_id=request_id,
                    status="ERROR",
                    error="claim_token does not match request_id",
                )
            if row["status"] == "CANCELED":
                return SubmissionResult(
                    request_id=request_id,
                    status="CANCELED",
                    error="request was explicitly canceled",
                )
            if row["status"] == "COMPLETED":
                if str(row["output_text"]) == output_text:
                    return SubmissionResult(request_id=request_id, status="IDEMPOTENT")
                return SubmissionResult(
                    request_id=request_id,
                    status="CONFLICT",
                    error="request was already completed with a different output",
                )

            connection.execute(
                """
                UPDATE translation_requests
                SET status = 'COMPLETED', output_text = ?, claimed_until = NULL,
                    updated_at = ?, completed_at = ?
                WHERE request_id = ? AND status IN ('PENDING', 'CLAIMED')
                """,
                (output_text, now, now, request_id),
            )
            return SubmissionResult(request_id=request_id, status="COMPLETED")

    def queue_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT run_id, status
                FROM translation_runs
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if run is None:
                return {
                    "run_id": None,
                    "status": "IDLE",
                    "pending": 0,
                    "claimed": 0,
                    "completed": 0,
                    "run_active": False,
                }
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM translation_requests
                    WHERE run_id = ?
                    GROUP BY status
                    """,
                    (run["run_id"],),
                ).fetchall()
            }
        status = str(run["status"])
        return {
            "run_id": str(run["run_id"]),
            "status": "TRANSLATING" if status == "ACTIVE" else status,
            "pending": counts.get("PENDING", 0),
            "claimed": counts.get("CLAIMED", 0),
            "completed": counts.get("COMPLETED", 0),
            "run_active": status == "ACTIVE",
        }
