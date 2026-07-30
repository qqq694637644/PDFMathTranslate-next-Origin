from __future__ import annotations

import json
from pathlib import Path

from script.export_gptaction_openapi import export_openapi


def read_server_url(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["servers"][0]["url"]


def test_export_reads_public_base_url_from_root_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PUBLIC_BASE_URL=https://dotenv.example.com/\n",
        encoding="utf-8",
    )
    output = tmp_path / "openapi/gpt-actions.openapi.json"

    exported = export_openapi(output=output)

    assert exported == output.resolve()
    assert read_server_url(output) == "https://dotenv.example.com"


def test_export_system_environment_overrides_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PUBLIC_BASE_URL=https://dotenv.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://system.example.com/")
    output = tmp_path / "openapi/gpt-actions.openapi.json"

    export_openapi(output=output)

    assert read_server_url(output) == "https://system.example.com"


def test_export_explicit_server_url_has_highest_priority(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PUBLIC_BASE_URL=https://dotenv.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://system.example.com")
    output = tmp_path / "openapi/gpt-actions.openapi.json"

    export_openapi(
        output=output,
        server_url="https://explicit.example.com/",
    )

    assert read_server_url(output) == "https://explicit.example.com"


def test_export_uses_code_default_without_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    output = tmp_path / "openapi/gpt-actions.openapi.json"

    export_openapi(output=output)

    assert read_server_url(output) == "https://translate.example.com"
