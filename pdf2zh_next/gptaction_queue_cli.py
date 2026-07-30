from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import GPTActionQueueError
from pdf2zh_next.translator.gptaction_queue import resolve_queue_db_path


class GPTActionQueueCLISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GPT_ACTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    queue_db: str | None = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2zh-action-queue",
        description="Inspect or recover the personal GPT Actions translation queue.",
    )
    parser.add_argument(
        "--queue-db",
        default=None,
        help=(
            "SQLite queue path. Defaults to GPT_ACTION_QUEUE_DB or "
            "~/.config/pdf2zh/gptaction-queue.sqlite3"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show the latest queue status")

    recover_parser = subparsers.add_parser(
        "recover-active-run",
        help="Mark one orphaned ACTIVE run as FAILED and release open claims",
    )
    recover_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive run-id confirmation",
    )
    return parser


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _recover_active_run(queue: GPTActionQueue, *, assume_yes: bool) -> int:
    summary = queue.get_active_run_summary()
    if summary is None:
        print("No ACTIVE GPT Action translation run was found.")
        return 0

    _print_json(summary)
    run_id = str(summary["run_id"])
    if not assume_yes:
        confirmation = input(
            "Type the ACTIVE run_id shown above to mark it FAILED: "
        ).strip()
        if confirmation != run_id:
            print("Confirmation did not match; no changes were made.")
            return 1

    recovered = queue.recover_active_run(run_id)
    _print_json(
        {
            "run_id": run_id,
            "status": "FAILED",
            **recovered,
            "message": (
                "Completed results were preserved. Open requests can be rebound "
                "by fingerprint when the next GPTAction translation starts."
            ),
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    explicit_values = {"queue_db": args.queue_db} if args.queue_db is not None else {}
    settings = GPTActionQueueCLISettings(**explicit_values)
    queue_path: Path = resolve_queue_db_path(settings.queue_db)
    queue = GPTActionQueue(queue_path)

    try:
        if args.command == "status":
            _print_json(queue.queue_status())
            return 0
        if args.command == "recover-active-run":
            return _recover_active_run(queue, assume_yes=args.yes)
    except (GPTActionQueueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
