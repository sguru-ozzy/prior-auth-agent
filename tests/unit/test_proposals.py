"""Field meanings, ambiguity and workflow authorization use reviewed metadata."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from prior_auth_agent.agent.inspection import authorize
from prior_auth_agent.agent.proposals import (
    Control,
    Observation,
    Proposal,
    candidates,
    load_browser_settings,
    validate_proposal,
)
from prior_auth_agent.errors import InputProblem


def observation(label: str, section: str = "Patient") -> Observation:
    return Observation(
        fingerprint="current",
        controls=[
            Control(
                ref="c0", label=label, role="textbox", input_type="text", section=section, enabled=True, has_value=False
            )
        ],
        authenticated=False,
    )


@pytest.mark.parametrize("label", ["First name", "Given name", "  GIVEN   NAME "])
def test_reviewed_aliases_bind_to_same_fact(label: str) -> None:
    page = observation(label)
    assert candidates(page, load_browser_settings())[0].binding_id == "patient.first_name"


@pytest.mark.parametrize("section", ["Prescriber", "Subscriber", ""])
def test_same_label_with_wrong_or_missing_subject_never_binds(section: str) -> None:
    assert candidates(observation("Given name", section), load_browser_settings()) == []


def test_unknown_wording_does_not_become_a_new_mapping() -> None:
    assert candidates(observation("Preferred name"), load_browser_settings()) == []


def test_two_matching_controls_are_ambiguous() -> None:
    page = observation("Given name")
    page = page.model_copy(update={"controls": [page.controls[0], page.controls[0].model_copy(update={"ref": "c1"})]})
    assert candidates(page, load_browser_settings()) == []


def test_fact_binding_cannot_authorize_a_write() -> None:
    assert authorize(observation("First name"), load_browser_settings(), set()) == []


@pytest.mark.parametrize(
    "update,code",
    [
        ({"observation": "old"}, "stale_proposal"),
        ({"binding_id": "invented"}, "unreviewed_binding"),
        ({"control_ref": "missing"}, "unreviewed_binding"),
        ({"action": "wait"}, "invalid_proposal"),
    ],
)
def test_unsafe_proposal_is_rejected(update: dict[str, str], code: str) -> None:
    page = observation("First name")
    values = {"action": "bind", "observation": "current", "binding_id": "patient.first_name", "control_ref": "c0"}
    values.update(update)
    with pytest.raises(InputProblem) as raised:
        validate_proposal(Proposal.model_validate(values), page, candidates(page, load_browser_settings()))
    assert raised.value.issue.code == code


@pytest.mark.parametrize("extra", ["value", "selector", "url", "javascript"])
def test_proposal_cannot_carry_values_or_executable_instructions(extra: str) -> None:
    with pytest.raises(ValidationError):
        Proposal.model_validate(
            {
                "action": "bind",
                "observation": "current",
                "binding_id": "patient.first_name",
                "control_ref": "c0",
                extra: "unsupported",
            }
        )


def test_strict_tool_schema_requires_nullable_properties() -> None:
    schema = Proposal.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_a_control_cannot_bind_to_two_different_facts() -> None:
    settings = load_browser_settings()
    fact = settings.bindings[-1]
    other = fact.model_copy(update={"id": "patient.last_name", "source": "patient.last_name"})
    settings = settings.model_copy(update={"bindings": [fact, other]})
    assert candidates(observation("First name"), settings) == []


def test_configuration_cannot_reference_an_invented_source(tmp_path: Path) -> None:
    config = load_browser_settings().model_dump()
    config["bindings"][-1]["source"] = "patient.invented"
    path = tmp_path / "browser.json"
    path.write_text(json.dumps(config))
    with pytest.raises(InputProblem) as raised:
        load_browser_settings(path)
    assert raised.value.issue.code == "browser_config_invalid"
