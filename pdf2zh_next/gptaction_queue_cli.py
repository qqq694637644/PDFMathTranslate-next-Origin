from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from pdf2zh_next.env_file import pydantic_env_file_kwargs
from pdf2zh_next.env_file import read_env_file_values
from pdf2zh_next.env_file import resolve_env_file_path
from pdf2zh_next.env_file import resolve_path_from_env_file
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import GPTActionQueueError
from pdf2zh_next.translator.gptaction_queue import resolve_queue_db_path

logger = logging.getLogger(__name__)


class GPTActionQueueCLISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GPT_ACTION_",
        extra="ignore",
    )

    queue_db: str | None = None


def load_queue_cli_settings(
    *,
    env_file: str | None = None,
    **explicit_values,
) -> GPTActionQueueCLISettings:
    resolved_env_file = resolve_env_file_path(env_file)
    if explicit_values.get("queue_db") is not None:
        explicit_values["queue_db"] = str(
            resolve_queue_db_path(explicit_values["queue_db"])
        )
    elif os.getenv("GPT_ACTION_QUEUE_DB"):
        explicit_values["queue_db"] = str(
            resolve_queue_db_path(os.environ["GPT_ACTION_QUEUE_DB"])
        )
    else:
        dotenv_queue_db = read_env_file_values(resolved_env_file).get(
            "GPT_ACTION_QUEUE_DB"
        )
        if dotenv_queue_db:
            explicit_values["queue_db"] = str(
                resolve_path_from_env_file(dotenv_queue_db, resolved_env_file)
            )
    return GPTActionQueueCLISettings(
        **explicit_values,
        **pydantic_env_file_kwargs(resolved_env_file),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2zh-action-queue",
        description="Inspect or recover the personal GPT Actions translation queue.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Shared dotenv path. Overrides PDF2ZH_ENV_FILE and the current .env.",
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
    invalidate_parser = subparsers.add_parser(
        "invalidate-request",
        help="Delete one completed cached result by request_id",
    )
    invalidate_parser.add_argument("request_id")
    invalidate_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive request-id confirmation",
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


def _invalidate_request(
    queue: GPTActionQueue,
    *,
    request_id: str,
    assume_yes: bool,
) -> int:
    summary = queue.get_completed_request(request_id)
    if summary is None:
        raise GPTActionQueueError(
            f"Completed GPT Action request was not found: {request_id}"
        )
    _print_json(summary)
    if not assume_yes:
        confirmation = input(
            "Type the completed request_id shown above to delete its cached result: "
        ).strip()
        if confirmation != request_id:
            print("Confirmation did not match; no changes were made.")
            return 1
    invalidated = queue.invalidate_completed_request(request_id)
    _print_json(
        {
            **invalidated,
            "status": "INVALIDATED",
            "message": "The completed output will not be reused by future runs.",
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)
    explicit_values = {"queue_db": args.queue_db} if args.queue_db is not None else {}
    settings = load_queue_cli_settings(
        env_file=args.env_file,
        **explicit_values,
    )
    queue_path: Path = resolve_queue_db_path(settings.queue_db)
    queue = GPTActionQueue(queue_path)
    logger.info(
        "GPT Action queue CLI: env_file=%s; queue=%s",
        resolve_env_file_path(args.env_file),
        queue_path,
    )

    try:
        if args.command == "status":
            _print_json(queue.queue_status())
            return 0
        if args.command == "recover-active-run":
            return _recover_active_run(queue, assume_yes=args.yes)
        if args.command == "invalidate-request":
            return _invalidate_request(
                queue,
                request_id=args.request_id,
                assume_yes=args.yes,
            )
    except (GPTActionQueueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
