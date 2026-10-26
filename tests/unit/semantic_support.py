"""Recorded model responses exercise the installed SDK and semantic policy together."""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx2
from openai import OpenAI
from pydantic import JsonValue

from prior_auth_agent.agent.planner import ModelPlanner
from prior_auth_agent.agent.proposals import load_browser_settings
from prior_auth_agent.agent.questions import load_workflow_settings
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.semantic.bindings import BindingResolver
from prior_auth_agent.semantic.contracts import UIControl, UISnapshot, load_semantic_settings
from prior_auth_agent.semantic.planner import SemanticPlanner
from prior_auth_agent.semantic.policy import FieldPolicy
from prior_auth_agent.storage.audit import ActionLog

Reply = Callable[[dict[str, JsonValue]], dict[str, JsonValue]]


def control(label: str = "Legal forename", **changes: object) -> UIControl:
    values = dict(
        ref="u0",
        label=label,
        group="",
        evidence="",
        region_text="",
        context="Patient details",
        help="",
        role="textbox",
        kind="text",
        value="",
        checked=False,
        invalid=False,
        enabled=True,
        required=True,
        href="",
        form_action="",
        form_method="",
        options=[],
    )
    values.update(changes)
    return UIControl.model_validate(values)


def snapshot(*controls: UIControl) -> UISnapshot:
    return UISnapshot(
        url="http://synthetic.test/form",
        title="",
        text="Patient details",
        alerts=[],
        controls=list(controls),
        pairs=[],
        tables=[],
        busy=False,
        truncated=False,
    )


def binding_reply(source: str, **changes: JsonValue) -> Reply:
    def reply(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        field = payload["field"]
        assert isinstance(field, dict)
        evidence = [field["label"], field["group"], field["help"], field["context"]]
        result: dict[str, JsonValue] = dict(
            observation=payload["observation"], binding_id=source, evidence=[text for text in evidence if text]
        )
        result.update(changes)
        return result

    return reply


@dataclass
class Harness:
    policy: FieldPolicy
    planner: SemanticPlanner
    captured: list[dict[str, JsonValue]]


@contextmanager
def harness(directory: Path, facts: dict[str, JsonValue], reply: Reply) -> Iterator[Harness]:
    captured: list[dict[str, JsonValue]] = []

    def response(request: httpx2.Request) -> httpx2.Response:
        request_body = json.loads(request.content)
        captured.append(request_body)
        payload = json.loads(request_body["input"][1]["content"])
        return httpx2.Response(
            200,
            json={
                "id": "resp_fixture",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_fixture",
                        "call_id": "call_fixture",
                        "name": request_body["tool_choice"]["name"],
                        "arguments": json.dumps(reply(payload)),
                    }
                ],
            },
        )

    settings = load_browser_settings()
    model = ModelPlanner(
        settings,
        "synthetic-key",
        OpenAI(
            api_key="synthetic-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(response))
        ),
    )
    log = ActionLog(directory / "actions.jsonl")
    planner = SemanticPlanner(
        model,
        settings,
        load_semantic_settings(),
        directory / "plans",
        facts,
        Credentials(username="SYNTHETIC_LOGIN", password="SYNTHETIC_PASSWORD"),
        log,
        time.monotonic() + 60,
    )
    try:
        yield Harness(
            FieldPolicy(facts, planner.policy, load_workflow_settings(), BindingResolver(planner)), planner, captured
        )
    finally:
        model.close()
        log.close()
