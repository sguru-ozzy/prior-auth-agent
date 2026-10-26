"""Observe → authorize → propose → execute → verify for unfamiliar portal interfaces."""

import copy
import re

from playwright.sync_api import expect
from pydantic import JsonValue

from prior_auth_agent.agent.planner import ModelPlanner
from prior_auth_agent.agent.proposals import BrowserSettings, digest, normalize, stop
from prior_auth_agent.agent.questions import WorkflowSettings
from prior_auth_agent.agent.recovery import History, RecoveryGuard
from prior_auth_agent.browser.session import PortalBrowser
from prior_auth_agent.contracts import Credentials, RunResult
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.semantic.browser import SemanticBrowser
from prior_auth_agent.semantic.contracts import SemanticSettings, UIAction, UIControl, UISnapshot, matches
from prior_auth_agent.semantic.history import (
    SemanticHistoryReader,
    detail_value,
    receipt,
    request_id,
    same_value,
    unique_control,
)
from prior_auth_agent.semantic.planner import SemanticPlanner
from prior_auth_agent.semantic.policy import FieldPolicy
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.validation import get_fact


class SemanticWorkflow:
    def __init__(
        self,
        browser: PortalBrowser,
        guard: RecoveryGuard,
        settings: SemanticSettings,
        browser_settings: BrowserSettings,
        workflow_settings: WorkflowSettings,
        facts: dict[str, JsonValue],
        credentials: Credentials,
        log: ActionLog,
        model: ModelPlanner | None,
    ) -> None:
        self.browser = browser
        self.guard = guard
        self.settings = settings
        self.credentials = credentials
        self.log = log
        self.ui = SemanticBrowser(browser, settings, log)
        self.policy = FieldPolicy(facts, settings, workflow_settings)
        self.planner = SemanticPlanner(
            model,
            browser_settings,
            settings,
            guard.store.directory / "semantic-plans",
            facts,
            credentials,
            log,
            browser.deadline,
        )
        self.fields: dict[str, JsonValue] = {}
        self.searched: dict[str, str] = {}
        self.selected: set[str] = set()
        self.manual_patient = False
        self.forms_searched = False
        self.answers: dict[str, JsonValue] = {}
        self.answers_verified = False
        self.answer_views: set[str] = set()
        self.case_verified = False
        self.review_url: str | None = None
        self.visited: set[tuple[str, str, str]] = set()
        self.initial_history_ids: set[str] = set()

    def fact(self, field: str) -> str:
        try:
            value = get_fact(self.guard.facts, field)
        except KeyError:
            value = None
        if not isinstance(value, str) or not value:
            raise stop(
                "source_fact_missing", "A required supplied value is missing.", "Provide the required fact.", field
            )
        return value

    def _has_fact(self, evidence: str, source: str) -> bool:
        value = self.fact(source)
        values = [value]
        if source.endswith("date_of_birth"):
            from_date = value.split("-")
            if len(from_date) == 3:
                values.append(from_date[1] + "/" + from_date[2] + "/" + from_date[0])
        return any(re.search(r"(?<![\w])" + re.escape(normalize(v)) + r"(?![\w])", normalize(evidence)) for v in values)

    def authenticate(self) -> None:
        submitted = False
        password_filled: tuple[str, str] | None = None
        for _ in range(12):
            snapshot = self.ui.stable()
            username = [
                c
                for c in snapshot.controls
                if matches(c.label, self.settings.intents["username"])
                and c.role == "textbox"
                and c.kind != "password"
                and c.enabled
            ]
            password = [
                c
                for c in snapshot.controls
                if c.kind == "password" and c.enabled and matches(c.label, self.settings.intents["password"])
            ]
            if len(username) > 1 or len(password) > 1:
                raise stop(
                    "authentication_ambiguous",
                    "Sign-in fields are ambiguous.",
                    "Review the sign-in page.",
                    "portal.access",
                )
            if not username and not password:
                if submitted and unique_control(snapshot, self.settings.intents["history"]) is not None:
                    self.log.event("semantic_authentication_verified")
                    return
                raise stop(
                    "authentication_unverified",
                    "The page does not prove a completed sign-in.",
                    "Review authentication and trusted origins.",
                    "portal.access",
                )
            actions = []
            for control in username:
                value = self.credentials.username.get_secret_value()
                if control.value != value:
                    actions.append(UIAction(intent="username", target=control.ref, value=value, method="fill"))
            for control in password:
                if password_filled != (control.ref, control.form_action):
                    actions.append(
                        UIAction(
                            intent="password",
                            target=control.ref,
                            value=self.credentials.password.get_secret_value(),
                            method="fill",
                        )
                    )
            if actions:
                action = self.planner.choose("authentication", snapshot, actions)
                current = snapshot.control(action.target)
                if current.form_method == "GET":
                    raise stop(
                        "credential_destination_unverified",
                        "The login form would place credentials in a URL.",
                        "Review the login form.",
                        "portal.access",
                    )
                self.ui.perform(snapshot, action)
                if action.intent == "password":
                    password_filled = (current.ref, current.form_action)
                continue
            login = [
                c
                for c in snapshot.controls
                if c.enabled
                and c.role == "button"
                and matches(c.label, self.settings.intents["login"])
                and (
                    not (username or password)[0].form_action or c.form_action == (username or password)[0].form_action
                )
            ]
            if len(login) != 1:
                raise stop(
                    "authentication_ambiguous",
                    "There is no unique supported sign-in action.",
                    "Review the sign-in controls.",
                    "portal.access",
                )
            for control in username + password:
                secret = self.credentials.password if control.kind == "password" else self.credentials.username
                expect(self.ui.target(snapshot, control.ref)).to_have_value(
                    secret.get_secret_value(), timeout=self.browser.timeout()
                )
            self.ui.perform(snapshot, UIAction(intent="login", target=login[0].ref), write=True)
            submitted = True
            password_filled = None
        raise stop(
            "authentication_budget",
            "Sign-in exceeded its bounded action limit.",
            "Review the login flow.",
            "portal.access",
        )

    def history(self, *, preserve_page: bool = False) -> History:
        if not preserve_page:
            history_reader = SemanticHistoryReader(self.ui, self.guard, self.settings)
            history = history_reader.read()
            self.initial_history_ids = history_reader.seen_ids.copy()
            return history
        # Read history in a code-created page so the current draft, questions and
        # attestation stay intact. Unexpected site-created pages remain blocked.
        self.browser.context.remove_listener("page", self.browser._popup)
        try:
            page = self.browser.context.new_page()
        finally:
            self.browser.context.on("page", self.browser._popup)
        original = self.browser.page
        reader: SemanticBrowser | None = None
        try:
            page.on("dialog", self.browser._dialog)
            page.on("response", self.browser._read_response)
            self.browser.page = page
            reader = SemanticBrowser(self.browser, self.settings, self.log)
            reader.actions = self.ui.actions
            reader.navigate(self.ui.page.url)
            return SemanticHistoryReader(reader, self.guard, self.settings).read()
        finally:
            if reader is not None:
                self.ui.actions = reader.actions
            self.browser.page = original
            page.close()

    def _intent(self, snapshot: UISnapshot, intent: str) -> list[UIAction]:
        actions = [
            UIAction(intent=intent, target=c.ref)
            for c in snapshot.controls
            if c.enabled
            and c.role in ("button", "link", "tab")
            and matches(c.label, self.settings.intents[intent])
            and (snapshot.fingerprint, UIAction(intent=intent, target=c.ref).id) not in self.ui.executed
        ]
        if intent == "new_request" and len(actions) > 1:
            controls = [snapshot.control(action.target) for action in actions]
            if all(c.role == "link" and c.href for c in controls) and len({c.href for c in controls}) == 1:
                actions = actions[:1]
        if intent in ("create", "send", "new_request", "new_patient") and len(actions) > 1:
            raise stop(
                "workflow_target_ambiguous",
                "An irreversible or identity-changing action is ambiguous.",
                "Review the matching controls manually.",
                "portal." + intent,
            )
        return actions

    def _current_search(self, snapshot: UISnapshot, source: str) -> UIControl | None:
        controls = [
            c
            for c in snapshot.controls
            if c.enabled
            and c.role in ("textbox", "combobox")
            and matches(c.label, self.settings.search_fields[source])
            and c.value == self.fact(source)
        ]
        return controls[0] if len(controls) == 1 else None

    def _search_actions(self, snapshot: UISnapshot) -> list[UIAction]:
        actions = []
        for source, labels in self.settings.search_fields.items():
            controls = [
                c
                for c in snapshot.controls
                if c.enabled and c.role in ("textbox", "combobox") and matches(c.label, labels)
            ]
            if len(controls) > 1:
                raise stop(
                    "search_ambiguous",
                    "More than one field could run this search.",
                    "Review the search controls.",
                    source,
                )
            if controls and source not in self.searched:
                actions.append(
                    UIAction(
                        intent="search", target=controls[0].ref, source=source, value=self.fact(source), method="fill"
                    )
                )
        return actions

    def _selection_actions(self, snapshot: UISnapshot) -> list[UIAction]:
        options = [c for c in snapshot.controls if c.enabled and c.role in ("option", "radio")]
        actions = []
        for source, companions, intent in (
            (
                "patient.member_id",
                ["patient.date_of_birth", "patient.first_name", "patient.last_name"],
                "select_patient",
            ),
            ("medication.name", ["medication.package"], "select_medication"),
        ):
            if source not in self.searched or intent in self.selected or self._current_search(snapshot, source) is None:
                continue
            sources = [source] + companions
            candidates = [c for c in options if all(self._has_fact(c.evidence or c.label, field) for field in sources)]
            if len(candidates) > 1:
                raise stop(
                    "search_match_ambiguous",
                    "Multiple search results match the supplied facts.",
                    "Review the matches manually.",
                    source,
                )
            if candidates:
                actions.append(UIAction(intent=intent, target=candidates[0].ref, source=source))
        if self.forms_searched and "select_form" not in self.selected:
            forms = [c for c in options if matches(c.group, self.settings.intents["form_choices"])]
            coverage_sources = ("coverage.bin", "coverage.pcn", "coverage.group")
            direct = [
                c for c in forms if all(self._has_fact(c.evidence or c.label, field) for field in coverage_sources)
            ]
            # A single form from a completed, source-bound form lookup still requires
            # the supplied plan name. The final saved review must prove BIN/PCN/group.
            if not direct and len(forms) == 1:
                try:
                    plan_name = self.fact("coverage.plan_name")
                except InputProblem:
                    plan_name = ""
                direct = [c for c in forms if plan_name and normalize(plan_name) in normalize(c.evidence or c.label)]
            if len(direct) != 1 and forms:
                raise stop(
                    "plan_match_ambiguous",
                    "The forms do not identify one supported match.",
                    "Review the plan form against BIN, PCN and group.",
                    "coverage.bin",
                )
            if direct:
                actions.append(UIAction(intent="select_form", target=direct[0].ref))
            native = [
                c
                for c in snapshot.controls
                if c.enabled and c.options and matches(c.label, self.settings.intents["form_choices"])
            ]
            for control in native:
                eligible = [option for option in control.options if not option.disabled and option.value]
                selected = [
                    option
                    for option in eligible
                    if all(self._has_fact(option.label, field) for field in coverage_sources)
                ]
                if not selected and len(eligible) == 1:
                    try:
                        plan_name = self.fact("coverage.plan_name")
                    except InputProblem:
                        plan_name = ""
                    selected = [
                        option for option in eligible if plan_name and normalize(plan_name) in normalize(option.label)
                    ]
                if len(selected) != 1:
                    raise stop(
                        "plan_match_ambiguous",
                        "The native form selector has no unique source-supported option.",
                        "Review the form against BIN, PCN and group.",
                        "coverage.bin",
                    )
                actions.append(
                    UIAction(intent="select_form", target=control.ref, method="select", value=selected[0].label)
                )
            if len([action for action in actions if action.intent == "select_form"]) > 1:
                raise stop(
                    "plan_match_ambiguous",
                    "More than one plan-form control could be selected.",
                    "Review the plan form controls.",
                    "coverage.bin",
                )
        return actions

    def _review(self, snapshot: UISnapshot) -> bool:
        required = list(self.settings.required_review_fields)
        try:
            self.fact("coverage.plan_name")
        except InputProblem:
            pass
        else:
            required.append("coverage.plan_name")
        for field in required:
            values = snapshot.values(self.settings.detail_labels.get(field, []))
            if not values:
                return False
            if len(values) != 1 or not same_value(values[0], self.fact(field), field):
                raise stop(
                    "saved_case_mismatch",
                    "A saved request value differs from its supplied source.",
                    "Review the request; do not alter the source to make it match.",
                    field,
                )
        self.case_verified = True
        self.review_url = snapshot.url
        self.log.event("semantic_saved_case_verified", observation=snapshot.fingerprint)
        return True

    def _intake_ready(self, snapshot: UISnapshot) -> bool:
        patient = "select_patient" in self.selected or (
            self.manual_patient
            and all(
                field in self.fields
                for field in (
                    "patient.first_name",
                    "patient.last_name",
                    "patient.date_of_birth",
                    "patient.member_id",
                    "patient.sex",
                    "patient.state",
                    "patient.zip_code",
                )
            )
        )
        medication = "select_medication" in self.selected or all(
            field in self.fields for field in ("medication.name", "medication.package")
        )
        plan = "select_form" in self.selected and all(
            field in self.fields for field in ("coverage.bin", "coverage.pcn", "coverage.group")
        )
        if patient and medication and plan:
            return True
        # Some portals expose a single direct-entry review instead of lookup widgets.
        for field in self.settings.required_review_fields:
            if field == "prescriber.npi":
                continue
            values = snapshot.values(self.settings.detail_labels.get(field, []))
            if len(values) != 1 or not same_value(values[0], self.fact(field), field):
                return False
        return True

    def _created(self, snapshot: UISnapshot) -> None:
        identifier = request_id(snapshot, self.settings)
        if identifier in self.initial_history_ids:
            raise stop(
                "existing_draft_selected",
                "New request opened a previously existing request.",
                "Review that request manually; the agent will not overwrite it.",
                "portal.request",
            )
        self.guard.created(identifier)
        self.log.event("semantic_created_id_verified", observation=digest(identifier))

    def _remember(self, source: str) -> None:
        self.fields[source] = copy.deepcopy(get_fact(self.guard.facts, source))

    def _verify_answers_after_save(
        self,
        current: UISnapshot,
        expected: dict[str, JsonValue],
        answer_url: str,
        view: str,
    ) -> UISnapshot:
        destination = current.url
        # Reload the page where answers were entered, including when a Save-and-continue
        # action advanced to a different URL. A verified view does not cover later questions.
        current = self.ui.navigate(answer_url)
        actions, observed = self.policy.fill_actions(current)
        if actions or not set(expected) <= set(observed):
            questions = unique_control(current, self.settings.intents["questions"])
            if questions is not None:
                current = self.ui.perform(current, UIAction(intent="questions", target=questions.ref))
        self.policy.verify(current, expected)
        self.answers.update(copy.deepcopy(expected))
        self.answer_views.add(view)
        self.answers_verified = True
        self.visited.clear()
        self.log.event("semantic_saved_answers_verified", observation=digest(expected))
        return self.ui.navigate(destination) if destination != answer_url else current

    def _send(self, snapshot: UISnapshot, action: UIAction) -> RunResult:
        if not self.case_verified or not self.answers_verified or snapshot.url != self.review_url:
            raise stop(
                "submission_proof_missing",
                "Saved case and answer checks are incomplete.",
                "Review the saved summary and questions before sending.",
                "portal.submission",
            )
        name = [c for c in snapshot.controls if matches(c.label, self.settings.detail_labels["attestation_name"])]
        if (
            len(name) != 1
            or name[0].value != self.fact("attestation_name")
            or not any(re.search(pattern, snapshot.text) for pattern in self.settings.attestation_patterns)
        ):
            raise stop(
                "attestation_unverified",
                "The full-name attestation or its meaning cannot be verified.",
                "Review and complete the attestation manually.",
                "attestation_name",
            )
        if any(
            c.enabled
            and c.role == "checkbox"
            and not c.checked
            and any(re.search(pattern, c.label) for pattern in self.settings.attestation_patterns)
            for c in snapshot.controls
        ):
            raise stop(
                "attestation_unverified",
                "The displayed accuracy attestation is not checked.",
                "Review the attestation before sending.",
                "attestation_name",
            )
        if any(c.invalid for c in snapshot.controls) or any(
            re.search(pattern, alert) for pattern in self.settings.blocking_alert_patterns for alert in snapshot.alerts
        ):
            raise stop(
                "portal_validation_rejected",
                "The portal still shows a validation error before Send.",
                "Review the portal error; do not change source facts to make it pass.",
                "portal.validation",
            )
        self._remember("attestation_name")
        history = self.history(preserve_page=True)
        if self.ui.snapshot().fingerprint != snapshot.fingerprint:
            raise stop(
                "page_changed", "The submission page changed during the duplicate check.", "Review the current request."
            )
        self.guard.before_send(history, self.fields)
        current = self.ui.perform(snapshot, action, write=True)
        if self.ui.last_write_status is None:
            raise stop(
                "submission_unverified",
                "No submission response was observed.",
                "Inspect this request manually; preserve the journal and do not repeat Send.",
                "portal.submission",
            )
        current = self.ui.navigate(current.url)
        if request_id(current, self.settings) != self.guard.journal.request_id:
            raise stop(
                "submission_identity_mismatch",
                "The final page identifies a different request.",
                "Inspect the saved request and confirmation manually.",
                "portal.submission",
            )
        self._review(current)
        status = detail_value(current, self.settings, "status")
        confirmation = receipt(current, self.settings)
        if matches(status, self.settings.submitted_statuses) and confirmation:
            result = RunResult(
                status="SUCCESS",
                reason="The same saved request shows a sent status and confirmation.",
                confirmation_id=confirmation,
                fields_filled=self.fields,
            )
        elif matches(status, self.settings.rejected_statuses) and confirmation is None:
            result = RunResult(
                status="FAILED",
                reason="The portal explicitly rejected the validated submission.",
                fields_filled=self.fields,
            )
        else:
            raise stop(
                "submission_unverified",
                "The portal does not show one verified sent or rejected outcome.",
                "Inspect this request manually; do not repeat Send.",
                "portal.submission",
            )
        self.guard.finish(result, request_id=self.guard.journal.request_id or "")
        return result

    def run(
        self,
        *,
        complete: bool,
        resume_request: str | None = None,
        authenticated: bool = False,
    ) -> RunResult:
        if not authenticated:
            self.authenticate()
        history = self.history()
        if resume_request is not None:
            self.guard.authorize_draft_resume(history, resume_request)
            raise stop(
                "semantic_resume_unsupported",
                "Unfamiliar-portal draft continuation requires operator review.",
                "Continue the existing draft manually; no new request was created.",
                "recovery.state",
            )
        cached = self.guard.inspect(history)
        if cached is not None:
            return cached
        if not complete:
            raise stop(
                "request_writes_disabled",
                "Sign-in and complete duplicate checks finished without request writes.",
                "Run without --inspect-portal to complete a request.",
            )
        snapshot = self.ui.stable()
        action = self.planner.choose("new_request", snapshot, self._intent(snapshot, "new_request"))
        self.guard.before_create(history)
        snapshot = self.ui.perform(snapshot, action, write=True)
        if snapshot.values(self.settings.detail_labels["request_id"]):
            self._created(snapshot)
        while self.ui.actions < self.settings.max_actions:
            phase = "intake" if self.guard.journal.phase == "CREATE_PENDING" else "questions_and_review"
            if phase != "intake":
                if request_id(snapshot, self.settings) != self.guard.journal.request_id:
                    raise stop(
                        "draft_identity_changed",
                        "The page no longer identifies this run's draft.",
                        "Review the current request before continuing.",
                        "portal.request",
                    )
                self._review(snapshot)
            fill_actions, observed = self.policy.fill_actions(snapshot)
            self.fields.update(copy.deepcopy(observed))
            if (
                self.case_verified
                and self.answers_verified
                and any(re.search(pattern, snapshot.text) for pattern in self.settings.attestation_patterns)
                and any(matches(c.label, self.settings.detail_labels["attestation_name"]) for c in snapshot.controls)
            ):
                fill_actions.extend(
                    UIAction(intent="attest", target=c.ref, source="attestation_name", value="true", method="check")
                    for c in snapshot.controls
                    if c.enabled
                    and c.role == "checkbox"
                    and not c.checked
                    and any(re.search(pattern, c.label) for pattern in self.settings.attestation_patterns)
                )
            if fill_actions:
                action = self.planner.choose(phase, snapshot, fill_actions)
                if (
                    phase == "intake"
                    and not self.manual_patient
                    and action.source is not None
                    and action.source.startswith("patient.")
                    and snapshot.control(action.target).value
                ):
                    raise stop(
                        "patient_context_unverified",
                        "The intake already contains different patient data.",
                        "Review the selected patient; do not overwrite an existing patient's details.",
                        action.source,
                    )
                snapshot = self.ui.perform(snapshot, action, write=True)
                if action.source:
                    self._remember(action.source)
                if action.source != "attestation_name":
                    self.case_verified = False
                    self.answers_verified = False
                    self.answer_views.clear()
                    self.visited.clear()
                continue
            searches = self._search_actions(snapshot) if phase == "intake" else []
            if searches:
                action = self.planner.choose("search", snapshot, searches)
                snapshot = self.ui.perform(snapshot, action, write=True, search=True)
                if action.source is None or self._current_search(snapshot, action.source) is None:
                    raise stop(
                        "search_not_current",
                        "The result cannot be tied to the visible current query.",
                        "Review the current search before selecting or entering a patient.",
                        action.source or "portal.search",
                    )
                self.searched[action.source] = digest(action.value)
                continue
            if (
                phase == "intake"
                and all(field in observed for field in ("coverage.bin", "coverage.pcn", "coverage.group"))
                and any(
                    c.options and matches(c.label, self.settings.intents["form_choices"]) for c in snapshot.controls
                )
            ):
                self.forms_searched = True
            choices = self._selection_actions(snapshot) if phase == "intake" else []
            if choices:
                action = self.planner.choose("select_result", snapshot, choices)
                snapshot = self.ui.perform(snapshot, action, write=True)
                self.selected.add(action.intent)
                used_fields = {
                    "select_patient": (
                        "patient.member_id",
                        "patient.date_of_birth",
                        "patient.first_name",
                        "patient.last_name",
                    ),
                    "select_medication": ("medication.name", "medication.package"),
                    "select_form": ("coverage.bin", "coverage.pcn", "coverage.group"),
                }
                for field in used_fields[action.intent]:
                    self._remember(field)
                continue
            if phase == "intake":
                patient_search = self._current_search(snapshot, "patient.member_id")
                no_matches = (
                    patient_search is not None
                    and any(
                        matches(line, self.settings.empty_search) for line in patient_search.region_text.splitlines()
                    )
                    and not any(
                        matches(c.label, self.settings.search_fields["medication.name"])
                        and c.context == patient_search.context
                        for c in snapshot.controls
                    )
                )
                if (
                    "patient.member_id" in self.searched
                    and no_matches
                    and not self.manual_patient
                    and "select_patient" not in self.selected
                ):
                    action = self.planner.choose("new_patient", snapshot, self._intent(snapshot, "new_patient"))
                    snapshot = self.ui.perform(snapshot, action, write=True)
                    self.manual_patient = True
                    continue
                form_actions = self._intent(snapshot, "find_forms") if not self.forms_searched else []
                if form_actions:
                    for field in ("coverage.bin", "coverage.pcn", "coverage.group"):
                        if field not in observed:
                            raise stop(
                                "plan_search_unverified",
                                "Plan search values cannot be read back.",
                                "Review BIN, PCN and group.",
                                field,
                            )
                    snapshot = self.ui.perform(
                        snapshot, self.planner.choose("find_forms", snapshot, form_actions), write=True
                    )
                    self.forms_searched = True
                    continue
                creates = self._intent(snapshot, "create")
                if creates:
                    if not self._intake_ready(snapshot):
                        raise stop(
                            "intake_unverified",
                            "The intake does not expose all required source matches.",
                            "Review the patient, medication and plan matches before Create.",
                            "portal.request",
                        )
                    snapshot = self.ui.perform(snapshot, self.planner.choose("create", snapshot, creates), write=True)
                    self._created(snapshot)
                    continue
            else:
                expected = {
                    field: value
                    for field, value in observed.items()
                    if field.startswith("clinical.")
                    or field in ("coverage.benefit", "coverage.subscriber_employee_id", "prescriber.npi")
                }
                view = digest(
                    {
                        "url": snapshot.url,
                        "questions": [
                            (control.label, control.group, rule.source, rule.format)
                            for control, rule, _, _ in self.policy.bindings(snapshot)
                            if rule.source in expected
                        ],
                    }
                )
                if expected and (not self.answers_verified or view not in self.answer_views):
                    answer_url = snapshot.url
                    saves = self._intent(snapshot, "save")
                    if saves:
                        snapshot = self.ui.perform(
                            snapshot, self.planner.choose("save_answers", snapshot, saves), write=True
                        )
                    elif self._intent(snapshot, "advance"):
                        advance = self.planner.choose("save_and_continue", snapshot, self._intent(snapshot, "advance"))
                        snapshot = self.ui.perform(snapshot, advance, write=True)
                    snapshot = self._verify_answers_after_save(snapshot, expected, answer_url, view)
                    continue
                sends = self._intent(snapshot, "send")
                if sends and self.case_verified and self.answers_verified and snapshot.url == self.review_url:
                    names = [
                        c
                        for c in snapshot.controls
                        if matches(c.label, self.settings.detail_labels["attestation_name"])
                    ]
                    action = self.planner.choose("submission", snapshot, sends)
                    if names:
                        return self._send(snapshot, action)
                    # Opening an attestation is read-only. A direct-submit endpoint is
                    # blocked here, rather than guessed or retried without a name.
                    control = snapshot.control(action.target)
                    if control.href or control.kind == "submit":
                        raise stop(
                            "attestation_missing",
                            "This submission control has no verified full-name attestation.",
                            "Review the attestation before sending.",
                            "attestation_name",
                        )
                    snapshot = self.ui.perform(snapshot, action)
                    continue
            navigation = []
            for intent in ("questions", "review", "advance"):
                navigation.extend(
                    action
                    for action in self._intent(snapshot, intent)
                    if (snapshot.url, action.intent, snapshot.control(action.target).label) not in self.visited
                )
            action = self.planner.choose(phase, snapshot, navigation)
            if (
                action.intent == "advance"
                and phase == "intake"
                and any(
                    matches(c.label, labels)
                    and field in self.searched
                    and not self.manual_patient
                    and ("select_patient" if field == "patient.member_id" else "select_medication") not in self.selected
                    for field, labels in self.settings.search_fields.items()
                    for c in snapshot.controls
                )
            ):
                raise stop(
                    "search_selection_unverified",
                    "A search has no verified selected result.",
                    "Review the current search before continuing.",
                    "portal.search",
                )
            self.visited.add((snapshot.url, action.intent, snapshot.control(action.target).label))
            snapshot = self.ui.perform(snapshot, action, write=action.intent == "advance")
            if self.guard.journal.phase == "CREATE_PENDING" and snapshot.values(
                self.settings.detail_labels["request_id"]
            ):
                self._created(snapshot)
        raise stop(
            "action_budget", "The semantic workflow reached its action limit.", "Review the saved draft and journal."
        )
