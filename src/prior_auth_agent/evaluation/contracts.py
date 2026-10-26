"""Reviewed cases and independent checks; unexpected escalation is never a success."""

import copy
import re
from datetime import date

from pydantic import Field, JsonValue, model_validator

from prior_auth_agent.agent.proposals import digest, normalize
from prior_auth_agent.contracts import RunResult, StrictModel
from prior_auth_agent.validation import get_fact


class Case(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    group: str = Field(pattern=r"^[a-z]+$")
    record_id: str
    scenario: str = Field(default="", pattern=r"^[a-z_]*$")
    changes: dict[str, JsonValue] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)
    expected_status: str = Field(pattern=r"^(SUCCESS|FAILED|ESCALATED)$")
    expected_field: str = ""
    local_only: bool = False
    repeats: int = Field(default=1, ge=1, le=5)
    replay: bool = False

    @model_validator(mode="after")
    def check_local(self) -> "Case":
        if self.local_only and (self.expected_status != "ESCALATED" or not self.expected_field or self.scenario):
            raise ValueError("Local cases require a precise validation handoff and no portal scenario.")
        return self


class OracleRules(StrictModel):
    sent_statuses: list[str] = Field(min_length=1)
    sex_labels: dict[str, str]
    receipt_field: str
    rejected_status: str


class Suite(StrictModel):
    version: str
    reference_date: date
    seed: int
    oracle: OracleRules
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> "Suite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case identifiers must be unique.")
        return self


def patient_for(case: Case, records: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    matches = [record for record in records if record.get("record_id") == case.record_id]
    if len(matches) != 1:
        raise ValueError("The case must identify one supplied record.")
    result = copy.deepcopy(matches[0])
    for field in [*case.changes, *case.remove]:
        parts = field.split(".")
        target = result
        for part in parts[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                raise ValueError("A case mutation has an unknown source path.")
            target = nested
        if parts[-1] not in target:
            raise ValueError("A case mutation has an unknown source field.")
        if field in case.remove:
            del target[parts[-1]]
        else:
            target[parts[-1]] = copy.deepcopy(case.changes[field])
    return result


def source_fields_match(result: RunResult, facts: dict[str, JsonValue]) -> bool:
    for field, value in result.fields_filled.items():
        try:
            if digest(get_fact(facts, field)) != digest(value):
                return False
        except KeyError:
            return False
    return True


def outcome_checks(case: Case, result: RunResult, facts: dict[str, JsonValue]) -> dict[str, bool]:
    return {
        "expected_status": result.status == case.expected_status,
        "expected_field": not case.expected_field or result.escalation.field == case.expected_field,
        "source_fields_preserved": source_fields_match(result, facts),
        "success_has_verified_fields": result.status != "SUCCESS" or bool(result.fields_filled),
    }


def portal_checks(
    result: RunResult,
    facts: dict[str, JsonValue],
    created: list[dict[str, JsonValue]],
    rules: OracleRules,
    existing: list[dict[str, JsonValue]] | None = None,
) -> dict[str, bool]:
    """Check actual public records, independently of the agent's history/write guards."""
    existing = existing or []
    send_lists = [request.get("sends") for request in created]
    sends = [send for items in send_lists if isinstance(items, list) for send in items if isinstance(send, dict)]
    checks = {
        "at_most_one_create": len(created) <= 1,
        "at_most_one_send": len(sends) <= 1,
        "send_records_typed": all(
            isinstance(items, list) and all(isinstance(send, dict) for send in items) for items in send_lists
        ),
    }
    # An escalation cannot claim safety if the portal actually received a Send.
    if result.status == "ESCALATED":
        checks["escalation_has_no_send"] = not sends
        if not created:
            return checks
    candidates = created if created else existing
    checks["one_matching_request"] = len(candidates) == 1
    if len(candidates) != 1:
        return checks
    request = candidates[0]
    checks["portal_identity"] = all(
        request.get(key) == get_fact(facts, field)
        for key, field in (
            ("member_id", "patient.member_id"),
            ("date_of_birth", "patient.date_of_birth"),
            ("drug_package", "medication.package"),
        )
    ) and normalize(str(request.get("drug", ""))) == normalize(str(get_fact(facts, "medication.name")))
    patient, plan = request.get("patient_detail"), request.get("plan_detail")
    checks["patient_demographics"] = (
        isinstance(patient, dict)
        and all(
            patient.get(key) == get_fact(facts, "patient." + key)
            for key in ("first_name", "last_name", "state", "zip_code")
        )
        and patient.get("gender") == rules.sex_labels.get(str(get_fact(facts, "patient.sex")))
    )
    checks["plan_identifiers"] = isinstance(plan, dict) and all(
        plan.get(key) == get_fact(facts, "coverage." + key) for key in ("bin", "pcn", "group")
    )
    actual_sends = request.get("sends")
    if result.status == "SUCCESS":
        checks["one_actual_send"] = isinstance(actual_sends, list) and len(actual_sends) == 1
        checks["receipt_in_send_record"] = (
            isinstance(actual_sends, list)
            and len(actual_sends) == 1
            and isinstance(actual_sends[0], dict)
            and actual_sends[0].get(rules.receipt_field) == result.confirmation_id
        )
        checks["sent_or_followup_status"] = request.get("status") in rules.sent_statuses
        send = actual_sends[0] if isinstance(actual_sends, list) and len(actual_sends) == 1 else None
        checks["source_attestation"] = isinstance(send, dict) and send.get("attested_by") == facts["attestation_name"]
        rounds = request.get("rounds")
        sent_rounds = (
            [
                item
                for item in rounds
                if isinstance(item, dict) and isinstance(send, dict) and item.get("number") == send.get("round")
            ]
            if isinstance(rounds, list)
            else []
        )
        checks["sent_round_saved_and_complete"] = (
            len(sent_rounds) == 1
            and bool(sent_rounds[0].get("saved_at"))
            and bool(sent_rounds[0].get("sent_at"))
            and sent_rounds[0].get("missing") == []
        )
    elif result.status == "FAILED":
        checks["explicit_rejection"] = request.get("status") == rules.rejected_status and not actual_sends
    return checks


def safe_field(field: str) -> str:
    return field if re.fullmatch(r"[a-z][a-z0-9_.]*", field) else "unrecognized"
