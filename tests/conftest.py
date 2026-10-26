"""Synthetic fixtures derived from the exercise's supplied example."""

import json
import socket
from pathlib import Path

import pytest
from pydantic import JsonValue

from prior_auth_agent.validation import Settings, load_settings


@pytest.fixture
def record() -> dict[str, JsonValue]:
    return json.loads((Path(__file__).parent / "fixtures" / "patient.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return load_settings(None)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not contact an external portal or model.")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    monkeypatch.setattr(socket, "create_connection", reject_connection)
