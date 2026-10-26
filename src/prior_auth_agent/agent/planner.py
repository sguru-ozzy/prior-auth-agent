"""One constrained Responses API call per proposal; no hidden retries or model fallback."""

import json
from importlib.resources import files
from typing import Protocol

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
from pydantic import JsonValue, ValidationError

from prior_auth_agent.agent.proposals import BrowserSettings, Candidate, Observation, Proposal, stop

PROMPT = files("prior_auth_agent.agent").joinpath("planner_prompt.txt").read_text(encoding="utf-8")


def _unique_proposal(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate proposal key.")
        result[key] = value
    return result


class Planner(Protocol):
    def propose(self, observation: Observation, authorized: list[Candidate], timeout: float) -> Proposal: ...


class ReviewedPlanner:
    def propose(self, observation: Observation, authorized: list[Candidate], timeout: float) -> Proposal:
        candidate = authorized[0] if authorized else None
        return Proposal(
            action="bind" if candidate else "escalate",
            observation=observation.fingerprint,
            binding_id=candidate.binding_id if candidate else None,
            control_ref=candidate.control_ref if candidate else None,
        )


class TransportProblem(Exception):
    def __init__(self, retryable: bool, retry_after: float = 0) -> None:
        super().__init__("Model transport failed.")
        self.retryable = retryable
        self.retry_after = retry_after


class ModelPlanner:
    def __init__(self, settings: BrowserSettings, api_key: str, client: OpenAI | None = None) -> None:
        self.settings = settings
        self.client = client or OpenAI(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=settings.model_timeout_seconds,
        )

    def close(self) -> None:
        self.client.close()

    def propose(self, observation: Observation, authorized: list[Candidate], timeout: float) -> Proposal:
        # Only reviewed candidate IDs leave this process. No raw labels, URL, values or credentials.
        payload = {
            "observation": observation.fingerprint,
            "has_controls": bool(observation.controls),
            "authorized": [candidate.model_dump() for candidate in authorized],
        }
        try:
            response = self.client.responses.create(
                model=self.settings.model,
                reasoning={"effort": self.settings.reasoning_effort},
                store=False,
                input=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                ],
                tools=[
                    {
                        "type": "function",
                        "name": "propose_action",
                        "description": "Propose one reviewed browser action.",
                        "strict": True,
                        "parameters": Proposal.model_json_schema(),
                    }
                ],
                tool_choice={"type": "function", "name": "propose_action"},
                parallel_tool_calls=False,
                max_output_tokens=1024,
                timeout=timeout,
            )
        except APIConnectionError:
            raise TransportProblem(True) from None
        except APIStatusError as error:
            retry_after = error.response.headers.get("retry-after", "0")
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                delay = 0.0
            raise TransportProblem(error.status_code in (408, 429) or error.status_code >= 500, delay) from None
        except OpenAIError:
            raise TransportProblem(False) from None
        output = response.output if isinstance(response.output, list) else []
        calls = [item for item in output if getattr(item, "type", None) == "function_call"]
        if response.status != "completed" or len(calls) != 1 or calls[0].name != "propose_action":
            raise stop(
                "model_response_invalid",
                "The model did not return one complete approved proposal.",
                "Review the model response contract before retrying.",
                "model.proposal",
            )
        if any(item.type not in ("function_call", "reasoning") for item in output):
            raise stop(
                "model_response_invalid",
                "The model returned unsupported output.",
                "Review the model response contract.",
                "model.proposal",
            )
        try:
            return Proposal.model_validate(json.loads(calls[0].arguments, object_pairs_hook=_unique_proposal))
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise stop(
                "model_response_invalid",
                "The model proposal does not match the approved schema.",
                "Review the proposal schema; no browser action was taken.",
                "model.proposal",
            ) from None
