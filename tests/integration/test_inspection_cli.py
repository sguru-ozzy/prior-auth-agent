"""Inspection must preserve input/key files and stop before browser access on bad input."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.proposals import DEFAULT_BROWSER_SETTINGS
from prior_auth_agent.cli import main


def arguments(tmp_path: Path, record: dict[str, JsonValue]) -> list[str]:
    patient = tmp_path / "patient.json"
    credentials = tmp_path / "credentials.json"
    patient.write_text(json.dumps(record))
    credentials.write_text('{"username":"synthetic","password":"SYNTHETIC_SECRET"}')
    return [
        "--portal-url",
        "http://127.0.0.1:1",
        "--patient",
        str(patient),
        "--credentials",
        str(credentials),
        "--out",
        str(tmp_path / "result.json"),
        "--as-of",
        "2026-10-25",
    ]


def test_missing_key_stops_before_opening_browser(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(arguments(tmp_path, record) + ["--inspect-portal", "--planner", "model"]) == 0
    assert json.loads((tmp_path / "result.json").read_text())["escalation"]["field"] == "model.configuration"


def test_invalid_patient_stops_before_browser_or_key_access(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_KEY_DO_NOT_LOG")
    patient = record["patient"]
    assert isinstance(patient, dict)
    patient["member_id"] = "invalid"
    assert main(arguments(tmp_path, record) + ["--inspect-portal", "--planner", "model"]) == 0
    text = (tmp_path / "result.json").read_text() + (tmp_path / "result.actions.jsonl").read_text()
    assert json.loads((tmp_path / "result.json").read_text())["escalation"]["field"] == "patient.member_id"
    assert "model_requested" not in text
    assert "SYNTHETIC_KEY" not in text


def test_browser_configuration_is_protected_from_overwrite(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    args = arguments(tmp_path, record)
    config = tmp_path / "browser.json"
    config.write_bytes(DEFAULT_BROWSER_SETTINGS.read_bytes())
    original = config.read_bytes()
    args[args.index("--out") + 1] = str(config)
    assert main(args + ["--inspect-portal", "--browser-config", str(config)]) == 2
    assert config.read_bytes() == original


def test_local_dotenv_is_protected_from_overwrite(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = arguments(tmp_path, record)
    monkeypatch.chdir(tmp_path)
    keyfile = tmp_path / ".env"
    keyfile.write_text("OPENAI_API_KEY=SYNTHETIC_KEY")
    args[args.index("--out") + 1] = str(keyfile)
    assert main(args + ["--inspect-portal"]) == 2
    assert keyfile.read_text() == "OPENAI_API_KEY=SYNTHETIC_KEY"


def test_browser_options_are_rejected_in_validation_only_mode(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments(tmp_path, record) + ["--validate-only", "--planner", "model"])
    assert raised.value.code == 2
