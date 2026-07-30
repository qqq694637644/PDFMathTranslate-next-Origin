from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from pdf2zh_next.gptaction_api import GPTActionAPISettings
from pdf2zh_next.gptaction_api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the Custom GPT Actions OpenAPI schema"
    )
    parser.add_argument(
        "--output",
        default="openapi/gpt-actions.openapi.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--server-url",
        default="https://example.com",
        help="Public HTTPS base URL used by Custom GPT",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        settings = GPTActionAPISettings(
            api_key="openapi-export-placeholder-key",
            queue_db=str(Path(temp_dir) / "queue.sqlite3"),
        )
        schema = create_app(settings).openapi()

    schema["servers"] = [
        {
            "url": args.server_url.rstrip("/"),
            "description": "Replace with the public HTTPS endpoint of pdf2zh-action-api",
        }
    ]
    output_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output_path.resolve())


if __name__ == "__main__":
    main()
