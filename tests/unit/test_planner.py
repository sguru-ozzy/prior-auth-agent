"""Exercise the actual SDK request/response contract without external API calls."""

import json

import httpx2
import pytest
from openai import OpenAI
from pydantic import JsonValue

from prior_auth_agent.agent.planner import ModelPlanner, TransportProblem
from prior_auth_agent.agent.proposals import Candidate, Control, Observation, load_browser_settings
from prior_auth_agent.errors import InputProblem


def test_sdk_sends_only_reviewed_references_and_enforces_constraints() -> None:
    captured = []
    proposal = {"action": "bind", "observation": "current", "binding_id": "auth.username", "control_ref": "c0"}

    def response(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_fixture",
                        "call_id": "call_fixture",
                        "name": "propose_action",
                        "arguments": json.dumps(proposal),
                    }
                ],
            },
        )

    client = OpenAI(
        api_key="synthetic-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(response))
    )
    settings = load_browser_settings()
    planner = ModelPlanner(settings, "synthetic-key", client)
    page = Observation(
        fingerprint="current",
        authenticated=False,
        controls=[
            Control(
                ref="c0",
                label="SYNTHETIC_PRIVATE_LABEL",
                role="textbox",
                input_type="text",
                section="SYNTHETIC_PRIVATE_CONTEXT",
                enabled=True,
                has_value=True,
            ),
        ],
    )
    try:
        assert (
            planner.propose(page, [Candidate(binding_id="auth.username", control_ref="c0")], 5).model_dump() == proposal
        )
    finally:
        planner.close()
    request = captured[0]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(request)
    assert "synthetic-key" not in json.dumps(request)
    assert request["model"] == settings.model
    assert request["store"] is False
    assert request["parallel_tool_calls"] is False
    assert request["tools"][0]["strict"] is True
    assert request["tool_choice"] == {"type": "function", "name": "propose_action"}


@pytest.mark.parametrize(
    "output,status",
    [
        ([], "completed"),
        ([], "incomplete"),
        (
            [
                {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "Cannot proceed."}],
                }
            ],
            "completed",
        ),
        ([{"type": "function_call", "id": "fc", "call_id": "c", "name": "unexpected", "arguments": "{}"}], "completed"),
        (
            [{"type": "function_call", "id": "fc", "call_id": "c", "name": "propose_action", "arguments": "{}"}],
            "completed",
        ),
        (
            [{"type": "function_call", "id": "fc", "call_id": "c", "name": "propose_action", "arguments": "{invalid"}],
            "completed",
        ),
        (
            [{"type": "function_call", "id": "fc", "call_id": "c", "name": "propose_action", "arguments": "{}"}] * 2,
            "completed",
        ),
    ],
)
def test_refusal_incomplete_or_malformed_output_never_becomes_an_action(
    output: list[dict[str, JsonValue]], status: str
) -> None:
    def response(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "resp", "status": status, "output": output})

    client = OpenAI(
        api_key="synthetic-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(response))
    )
    planner = ModelPlanner(load_browser_settings(), "synthetic-key", client)
    try:
        with pytest.raises(InputProblem, match="model"):
            planner.propose(Observation(fingerprint="current", controls=[], authenticated=False), [], 5)
    finally:
        planner.close()


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (500, True)])
def test_sdk_does_not_retry_internally(status: int, retryable: bool) -> None:
    calls = []

    def response(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            status, json={"error": {"message": "SYNTHETIC_PRIVATE_ERROR"}}, headers={"retry-after": "2"}
        )

    client = OpenAI(
        api_key="synthetic-key", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(response))
    )
    planner = ModelPlanner(load_browser_settings(), "synthetic-key", client)
    try:
        with pytest.raises(TransportProblem) as raised:
            planner.propose(Observation(fingerprint="current", controls=[], authenticated=False), [], 5)
        assert raised.value.retryable is retryable
        assert raised.value.retry_after == 2
        assert "PRIVATE" not in str(raised.value)
        assert len(calls) == 1
    finally:
        planner.close()
