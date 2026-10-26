"""Unknown wording needs a value-free proposal and deterministic authorization."""

import copy
import json
import time
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from prior_auth_agent.errors import InputProblem
from prior_auth_agent.semantic.contracts import BindingProposal, UIPair, load_semantic_settings
from tests.unit.semantic_support import binding_reply, control, harness, snapshot


def test_unknown_label_fills_source_and_replays_cache(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        page = snapshot(control())
        actions, observed = run.policy.fill_actions(page)
        assert observed == {}
        assert actions[0].source == "patient.first_name"
        assert actions[0].value == record["patient"]["first_name"]
        page = snapshot(control(value=actions[0].value))
        run.policy.verify(page, {"patient.first_name": record["patient"]["first_name"]})
        assert len(run.captured) == 1
        request = run.captured[0]
        assert request["store"] is False
        assert request["parallel_tool_calls"] is False
        assert request["tools"][0]["strict"] is True
        assert request["tool_choice"]["name"] == "propose_field_binding"
    private = json.dumps(request) + "".join(file.read_text() for file in (tmp_path / "plans").glob("*.json"))
    assert record["patient"]["first_name"] not in private
    assert record["patient"]["member_id"] not in private
    assert "SYNTHETIC_PASSWORD" not in private
    assert "SYNTHETIC_LOGIN" not in private
    assert "http://" not in private
    assert all(file.stat().st_mode & 0o077 == 0 for file in (tmp_path / "plans").glob("*.json"))
    with harness(tmp_path, record, binding_reply("patient.last_name/text")) as run:
        assert run.policy.fill_actions(snapshot(control()))[0][0].source == "patient.first_name"
        assert run.captured == []


@pytest.mark.parametrize(
    "label,context,kind,role,source",
    [
        ("Legal forename", "Prescriber details", "text", "textbox", "patient.first_name/text"),
        ("Legal forename", "Patient and subscriber", "text", "textbox", "patient.first_name/text"),
        ("Preferred first name", "Patient details", "text", "textbox", "patient.first_name/text"),
        ("Forename initials", "Patient details", "text", "textbox", "patient.first_name/text"),
        ("Legal forename", "Details", "text", "textbox", "patient.first_name/text"),
        ("Patient first name or surname", "Patient details", "text", "textbox", "patient.first_name/text"),
        ("Employee member identifier", "Patient details", "text", "textbox", "patient.member_id/text"),
        ("Quantity requested per 30 days", "Clinical", "number", "textbox", "clinical.quantity_per_28_days/text"),
        ("Quantity per 28 days or 14 days", "Clinical", "number", "textbox", "clinical.quantity_per_28_days/text"),
        ("Quantity requested", "Clinical", "number", "textbox", "clinical.quantity_per_28_days/text"),
        (
            "Body surface area percent above 10",
            "Clinical",
            "number",
            "textbox",
            "clinical.body_surface_area_percent/text",
        ),
        (
            "Percentage of body surface area involved",
            "Clinical",
            "select",
            "combobox",
            "clinical.body_surface_area_percent/text",
        ),
        ("Is the patient not starting therapy?", "Clinical", "checkbox", "checkbox", "clinical.new_start/boolean"),
        (
            "Is patient starting therapy within 30 days?",
            "Clinical",
            "checkbox",
            "checkbox",
            "clinical.new_start/boolean",
        ),
        ("Tuberculosis screening in last 12 months", "Clinical", "radio", "radio", "clinical.new_start/boolean"),
        ("Current disease severity", "Clinical", "text", "textbox", "clinical.body_surface_area_percent/text"),
    ],
)
def test_unsafe_meanings_stop_before_model(
    tmp_path: Path, record: dict, label: str, context: str, kind: str, role: str, source: str
) -> None:
    with harness(tmp_path, record, binding_reply(source)) as run:
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(
                snapshot(control(label, context=context, kind=kind, role=role, group=label if role == "radio" else ""))
            )
        assert raised.value.issue.code == "question_unreviewed"
        assert run.captured == []


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"binding_id": "patient.last_name/text"}, "binding_unverified"),
        ({"binding_id": "unknown.field/text"}, "binding_unverified"),
        ({"observation": "old"}, "stale_binding"),
        ({"evidence": []}, "binding_unverified"),
        ({"evidence": ["invented quote", "Patient"]}, "binding_unverified"),
        ({"evidence": ["e"]}, "binding_unverified"),
        ({"binding_id": None}, "question_unreviewed"),
        ({"value": "INVENTED_NAME"}, "model_response_invalid"),
        ({"selector": "#patient"}, "model_response_invalid"),
    ],
)
def test_invalid_proposals_never_fill(tmp_path: Path, record: dict, changes: dict, code: str) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text", **changes)) as run:
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(snapshot(control()))
        assert raised.value.issue.code == code
        assert len(run.captured) == 1


def test_saved_details_keep_conflicting_values(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        page = snapshot()
        page = page.model_copy(
            update={
                "pairs": [
                    UIPair(label="Legal forename", value="Unexpected", context="Patient details"),
                    UIPair(label="First name", value=record["patient"]["first_name"]),
                ]
            }
        )
        assert run.policy.resolver.values(page, "patient.first_name") == [record["patient"]["first_name"], "Unexpected"]
        assert len(run.captured) == 1


def test_duplicate_dynamic_targets_escalate(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(snapshot(control(), control(ref="u1")))
        assert raised.value.issue.code == "field_target_ambiguous"
        assert len(run.captured) == 1


def test_changed_subject_and_redaction_cannot_reuse_binding(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        assert run.policy.rule(control()).source == "patient.first_name"
        assert run.policy.rule(control(context="Prescriber")) is None
        assert run.policy.rule(control(context=record["patient"]["first_name"])) is None
        assert len(run.captured) == 1


@pytest.mark.parametrize(
    "source,label,kind,role,value,answer",
    [
        ("clinical.quantity_per_28_days", "Total quantity for 28 days", "number", "textbox", 0, "0"),
        (
            "clinical.sensitive_areas_affected",
            "Are sensitive body areas involved?",
            "checkbox",
            "checkbox",
            False,
            "false",
        ),
    ],
)
def test_zero_and_false_preserved(
    tmp_path: Path, record: dict, source: str, label: str, kind: str, role: str, value: JsonValue, answer: str
) -> None:
    facts = copy.deepcopy(record)
    facts["clinical"][source.split(".")[1]] = value
    fmt = "boolean" if role == "checkbox" else "text"
    with harness(tmp_path, facts, binding_reply(source + "/" + fmt)) as run:
        field = control(label, context="Clinical questions", kind=kind, role=role, checked=value is False)
        actions, _ = run.policy.fill_actions(snapshot(field))
        assert actions[0].source == source
        assert actions[0].value == answer


def test_missing_fact_is_not_generated(tmp_path: Path, record: dict) -> None:
    record["clinical"]["body_surface_area_percent"] = None
    with harness(tmp_path, record, binding_reply("clinical.body_surface_area_percent/text")) as run:
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(
                snapshot(control("Body surface area percentage involved", context="Clinical", kind="number"))
            )
        assert raised.value.issue.code == "answer_missing"


def test_model_budget_and_no_model(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        run.planner.calls = run.planner.policy.max_model_calls
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(snapshot(control()))
        assert raised.value.issue.code == "model_budget"
        assert run.captured == []
        run.planner.model = None
        assert run.policy.rule(control()) is None


def test_reviewed_aliases_skip_model(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.last_name/text")) as run:
        assert run.policy.fill_actions(snapshot(control("Given name")))[0][0].source == "patient.first_name"
        assert run.captured == []


def test_catalog_rejects_unreviewed_formats() -> None:
    settings = load_semantic_settings().model_dump()
    settings["binding_rules"][0]["field"]["format"] = "boolean"
    with pytest.raises(ValidationError):
        type(load_semantic_settings()).model_validate(settings)


def test_shared_transport_deadline(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        run.planner.deadline = time.monotonic() - 1
        with pytest.raises(InputProblem) as raised:
            run.planner.request({}, BindingProposal, "test", "propose_field_binding", 64)
        assert raised.value.issue.code == "model_budget"
        assert run.planner.calls == 0


def test_cached_proposal_is_revalidated(tmp_path: Path, record: dict) -> None:
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        run.policy.fill_actions(snapshot(control()))
        path = next((tmp_path / "plans").glob("*.json"))
        proposal = json.loads(path.read_text())
        proposal["binding_id"] = "prescriber.name/text"
        path.write_text(json.dumps(proposal))
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(snapshot(control()))
        assert raised.value.issue.code == "binding_unverified"
        assert len(run.captured) == 1


def test_unsafe_cache_permissions_stop_before_model(tmp_path: Path, record: dict) -> None:
    (tmp_path / "plans").mkdir(mode=0o755)
    with harness(tmp_path, record, binding_reply("patient.first_name/text")) as run:
        with pytest.raises(InputProblem) as raised:
            run.policy.fill_actions(snapshot(control()))
        assert raised.value.issue.code == "unsafe_plan_cache"
        assert run.captured == []
