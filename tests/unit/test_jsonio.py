"""Reject ambiguous JSON and preserve old output on publication failure."""

import json
import stat
from pathlib import Path

import pytest

from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage import jsonio


@pytest.mark.parametrize(
    "payload",
    [
        b'{"patient":{"member_id":"first","member_id":"second"}}',
        b'{"number":NaN}',
        b'{"number":Infinity}',
        b'{"number":-Infinity}',
        b'{"number":1e9999}',
        b'{"broken":}',
        b'{"bad":"\xff"}',
    ],
)
def test_ambiguous_or_invalid_json_is_rejected(tmp_path: Path, payload: bytes) -> None:
    source = tmp_path / "patient.json"
    source.write_bytes(payload)
    with pytest.raises(InputProblem) as raised:
        jsonio.load_object(source, "patient", 1024)
    assert raised.value.issue.field == "patient"
    assert raised.value.issue.code == "invalid_json"
    assert source.read_bytes() == payload


def test_array_is_not_one_patient(tmp_path: Path) -> None:
    source = tmp_path / "patients.json"
    source.write_text("[{}, {}]", encoding="utf-8")
    with pytest.raises(InputProblem) as raised:
        jsonio.load_object(source, "patient", 1024)
    assert raised.value.issue.code == "expected_object"


def test_input_size_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "patient.json"
    source.write_text(json.dumps({"text": "x" * 20}), encoding="utf-8")
    with pytest.raises(InputProblem) as raised:
        jsonio.load_object(source, "patient", 10)
    assert raised.value.issue.code == "input_too_large"


def test_unreadable_input_has_no_path_or_data_in_reason(tmp_path: Path) -> None:
    source = tmp_path / "sensitive-patient-identifier.json"
    with pytest.raises(InputProblem) as raised:
        jsonio.load_object(source, "patient", 1024)
    assert str(source) not in str(raised.value)
    assert raised.value.issue.code == "unreadable_input"


def test_atomic_result_is_private(tmp_path: Path) -> None:
    target = tmp_path / "new" / "result.json"
    jsonio.write_atomic(target, '{"status":"ESCALATED"}\n')
    assert target.read_text() == '{"status":"ESCALATED"}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_failed_replace_preserves_existing_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "result.json"
    target.write_text("previous result", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("simulated storage failure")

    monkeypatch.setattr(jsonio.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        jsonio.write_atomic(target, "new result")
    assert target.read_text() == "previous result"
    assert list(tmp_path.iterdir()) == [target]
