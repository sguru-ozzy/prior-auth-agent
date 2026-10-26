"""Question plans preserve source facts and stop on missing or unfamiliar meanings."""

import copy

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.questions import load_workflow_settings, plan_answers
from prior_auth_agent.errors import InputProblem


def question(text: str, kind: str = "text", key: str = "q", **changes: JsonValue) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {
        "id": key,
        "text": text,
        "kind": kind,
        "required": True,
        "choices": [],
        "show_if": None,
        "hint": "",
    }
    value.update(changes)
    return value


def test_answers_are_from_source_and_record_is_unchanged(record: dict[str, JsonValue]) -> None:
    before = copy.deepcopy(record)
    result = plan_answers(
        [
            question("Primary diagnosis (ICD-10 code)", key="diagnosis"),
            question("Is this a new start of therapy?", "radio", "start", choices=["Yes", "No"]),
            question("Which benefit covers this request?", "radio", "benefit", choices=["Part B", "Part D"]),
            question("Anything else the reviewer should know", "textarea", "notes", required=False),
        ],
        record,
        load_workflow_settings(),
    )
    assert result.answers == {"diagnosis": "L40.50", "start": "Yes", "benefit": "Part D", "notes": ""}
    assert result.fields["clinical.new_start"] is True
    assert "clinical.notes" not in result.fields
    assert record == before


def test_false_and_zero_are_not_missing(record: dict[str, JsonValue]) -> None:
    record["clinical"]["new_start"] = False
    record["clinical"]["quantity_per_28_days"] = 0
    result = plan_answers(
        [
            question("Is this a new start of therapy?", "radio", "start", choices=["Yes", "No"]),
            question("Quantity per 28 days", "number", "quantity"),
        ],
        record,
        load_workflow_settings(),
    )
    assert result.answers == {"start": "No", "quantity": "0"}
    assert result.fields["clinical.new_start"] is False
    assert result.fields["clinical.quantity_per_28_days"] == 0


def test_conditional_date_uses_reviewed_format_and_canonical_evidence(record: dict[str, JsonValue]) -> None:
    questions = [
        question(
            "When did the patient start this medication?", "date", "date", show_if={"question": "start", "answer": "No"}
        ),
        question("Is this a new start of therapy?", "radio", "start", choices=["Yes", "No"]),
    ]
    assert plan_answers(questions, record, load_workflow_settings()).answers == {"start": "Yes"}
    record["clinical"]["new_start"] = False
    record["clinical"]["current_therapy_start_date"] = "2026-09-05"
    result = plan_answers(questions, record, load_workflow_settings())
    assert result.answers == {"start": "No", "date": "09/05/2026"}
    assert result.stored_answers["date"] == "2026-09-05"
    assert result.fields["clinical.current_therapy_start_date"] == "2026-09-05"


@pytest.mark.parametrize("value", [None, ""])
def test_required_missing_answer_escalates(record: dict[str, JsonValue], value: JsonValue) -> None:
    record["clinical"]["site_of_care"] = value
    with pytest.raises(InputProblem) as raised:
        plan_answers(
            [question("Where will the patient receive this medication?", "radio")], record, load_workflow_settings()
        )
    assert raised.value.issue.field == "clinical.site_of_care"


def test_unknown_required_question_escalates_even_with_familiar_id(record: dict[str, JsonValue]) -> None:
    with pytest.raises(InputProblem) as raised:
        plan_answers(
            [question("Has the patient had tuberculosis screening?", key="q-diagnosis")],
            record,
            load_workflow_settings(),
        )
    assert raised.value.issue.code == "question_unreviewed"


def test_unknown_optional_question_stays_blank(record: dict[str, JsonValue]) -> None:
    plan = plan_answers([question("Unsupported optional question", required=False)], record, load_workflow_settings())
    assert plan.answers == {"q": ""}
    assert plan.fields == {}


def test_choice_must_match_source_without_silent_conversion(record: dict[str, JsonValue]) -> None:
    record["coverage"]["benefit"] = "Medicare"
    with pytest.raises(InputProblem) as raised:
        plan_answers(
            [question("Which benefit covers this request?", "radio", choices=["Part B", "Part D"])],
            record,
            load_workflow_settings(),
        )
    assert raised.value.issue.field == "coverage.benefit"


def test_reviewed_therapy_evidence_and_detail_are_preserved(record: dict[str, JsonValue]) -> None:
    plan = plan_answers(
        [
            question("Has the patient tried a conventional systemic therapy?", "radio", "tried", choices=["Yes", "No"]),
            question(
                "Which therapy, when did it start, and why was it stopped?",
                "textarea",
                "detail",
                show_if={"question": "tried", "answer": "Yes"},
            ),
        ],
        record,
        load_workflow_settings(),
    )
    assert plan.answers["tried"] == "Yes"
    assert plan.answers["detail"] == "Acitretin; started 2026-09-05; stopped: Not tolerated."
    assert plan.fields["clinical.prior_therapies"] == record["clinical"]["prior_therapies"]


@pytest.mark.parametrize(
    "therapies", [[], [{"name": "Unreviewed therapy", "started": "2026-09-05", "stopped_reason": "Unknown"}]]
)
def test_absent_or_unreviewed_therapy_does_not_become_no(record: dict[str, JsonValue], therapies: JsonValue) -> None:
    record["clinical"]["prior_therapies"] = therapies
    with pytest.raises(InputProblem) as raised:
        plan_answers(
            [question("Has the patient tried a conventional systemic therapy?", "radio")],
            record,
            load_workflow_settings(),
        )
    assert raised.value.issue.code == "therapy_evidence_unsupported"


@pytest.mark.parametrize("key", ["started", "stopped_reason"])
def test_therapy_detail_does_not_invent_missing_history(record: dict[str, JsonValue], key: str) -> None:
    record["clinical"]["prior_therapies"][0][key] = None
    with pytest.raises(InputProblem):
        plan_answers(
            [question("Which therapy, when did it start, and why was it stopped?", "textarea")],
            record,
            load_workflow_settings(),
        )


@pytest.mark.parametrize("condition", [{"question": "missing", "answer": "Yes"}, {"question": "q", "answer": "Yes"}])
def test_broken_or_cyclic_conditions_escalate(record: dict[str, JsonValue], condition: JsonValue) -> None:
    with pytest.raises(InputProblem) as raised:
        plan_answers([question("Primary diagnosis (ICD-10 code)", show_if=condition)], record, load_workflow_settings())
    assert raised.value.issue.code == "question_condition_invalid"


def test_duplicate_ids_escalate(record: dict[str, JsonValue]) -> None:
    with pytest.raises(InputProblem):
        plan_answers([question("Primary diagnosis (ICD-10 code)")] * 2, record, load_workflow_settings())


def test_non_systemic_treatment_is_not_presented_as_systemic_evidence(record: dict[str, JsonValue]) -> None:
    record["clinical"]["prior_therapies"].append(
        {"name": "Phototherapy", "started": "2026-01-01", "stopped_reason": "Stopped"}
    )
    plan = plan_answers(
        [question("Which therapy, when did it start, and why was it stopped?", "textarea")],
        record,
        load_workflow_settings(),
    )
    assert "Phototherapy" not in plan.answers["q"]
    assert plan.fields["clinical.prior_therapies"] == record["clinical"]["prior_therapies"]


def test_reports_both_missing_clinical_facts_without_inference(record: dict[str, JsonValue]) -> None:
    raw = [
        question(
            "Has the patient had a tuberculosis screening in the last 12 months?", "radio", "tb", choices=["Yes", "No"]
        ),
        question("Current disease severity", "radio", "severity", choices=["Mild", "Moderate", "Severe"]),
    ]
    with pytest.raises(InputProblem) as raised:
        plan_answers(raw, record, load_workflow_settings())
    issue = raised.value.issue
    assert issue.field == "portal.questions"
    assert "tuberculosis screening" in issue.problem
    assert "Current disease severity" in issue.problem
    assert issue.code == "answers_unavailable"


@pytest.mark.parametrize("employee_id", [None, "SYNTHETIC-EMPLOYEE-ID"])
def test_employee_question_uses_the_coverage_identifier_or_escalates(
    record: dict[str, JsonValue], employee_id: str | None
) -> None:
    record["coverage"]["subscriber_employee_id"] = employee_id
    schema = [question("State employee ID of the subscriber", "text")]
    if employee_id is None:
        with pytest.raises(InputProblem) as raised:
            plan_answers(schema, record, load_workflow_settings())
        assert raised.value.issue.field == "coverage.subscriber_employee_id"
    else:
        plan = plan_answers(schema, record, load_workflow_settings())
        assert next(iter(plan.answers.values())) == employee_id
        assert "patient.member_id" not in plan.fields
