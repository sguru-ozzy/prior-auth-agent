"""Portal A's reviewed browser adapter; task decisions remain source-bound and guarded."""

import re
from urllib.parse import urlsplit

from playwright.sync_api import Locator, expect
from pydantic import JsonValue

from prior_auth_agent.agent.proposals import digest, normalize, stop
from prior_auth_agent.agent.questions import AnswerPlan, WorkflowSettings, plan_answers
from prior_auth_agent.agent.recovery import History, RecoveryGuard
from prior_auth_agent.browser.feedback import validation_rejection
from prior_auth_agent.browser.requests import RequestHistoryReader
from prior_auth_agent.browser.session import PortalBrowser, origin
from prior_auth_agent.contracts import RunResult
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.validation import get_fact


class RequestWorkflow:
    def __init__(
        self,
        browser: PortalBrowser,
        guard: RecoveryGuard,
        settings: WorkflowSettings,
        facts: dict[str, JsonValue],
        log: ActionLog,
    ) -> None:
        self.browser = browser
        self.page = browser.page
        self.guard = guard
        self.settings = settings
        self.facts = facts
        self.log = log
        self.fields: dict[str, JsonValue] = {}
        parsed = urlsplit(browser.start_url)
        self.base = parsed.scheme + "://" + parsed.netloc
        self.request_id: str | None = None
        self.electronic: bool | None = None
        self.answer_plan: AnswerPlan | None = None

    def path(self, name: str) -> str:
        path = self.settings.paths[name].format(request_id=self.request_id or "")
        if not path.startswith("/api/") or "?" in path or ".." in path or "//" in path:
            raise stop(
                "workflow_path_invalid",
                "A reviewed workflow path is invalid.",
                "Review the configuration.",
                "workflow.config",
            )
        return path

    def read(self, name: str) -> dict[str, JsonValue]:
        url = self.base + self.path(name)
        for attempt in range(3):
            response = self.page.request.get(url, timeout=self.browser.timeout())
            if response.status in (429, 502, 503, 504) and attempt < 2:
                self.log.event("portal_read_retry", attempt=attempt + 1)
                self.page.wait_for_timeout(min(1000 * (attempt + 1), self.browser.timeout()))
                continue
            if not response.ok:
                raise stop(
                    "portal_read_rejected",
                    "A required portal read was rejected.",
                    "Review portal availability and the request state.",
                    "portal.access",
                )
            try:
                document = response.json()
            except ValueError:
                break
            if isinstance(document, dict):
                return document
            break
        raise stop(
            "portal_contract_unknown",
            "The portal returned an unsupported response.",
            "Review the portal adapter before continuing.",
            "portal.access",
        )

    def one(self, locator: Locator, field: str) -> Locator:
        if locator.count() != 1 or not locator.is_visible() or not locator.is_enabled():
            raise stop(
                "workflow_target_ambiguous",
                "A required control is missing, disabled or ambiguous.",
                "Review the current portal screen.",
                field,
            )
        return locator

    def click(self, locator: Locator, field: str) -> None:
        self.browser.observe()
        self.one(locator, field).click(timeout=self.browser.timeout())
        self.log.event("workflow_action_verified", binding=field, action="click")

    def fill(self, locator: Locator, value: str, field: str) -> None:
        self.browser.observe()
        self.one(locator, field).fill(value, timeout=self.browser.timeout())
        expect(locator).to_have_value(value, timeout=self.browser.timeout())
        self.log.event("workflow_action_verified", binding=field, action="fill")

    def write(
        self, locator: Locator, field: str, path_name: str, methods: tuple[str, ...], *, check_validation: bool = True
    ) -> int:
        self.browser.observe()
        for method in methods:
            self.browser.guard.permit_write(self.base, method, self.path(path_name))
        try:
            with self.page.expect_response(
                lambda response: (
                    response.request.method in methods
                    and origin(response.url) == origin(self.base)
                    and urlsplit(response.url).path == self.path(path_name)
                ),
                timeout=self.browser.timeout(),
            ) as response:
                self.click(locator, field)
            status = response.value.status
            if check_validation and status in (400, 422):
                raise validation_rejection(
                    self.browser, self.settings, field.removeprefix("portal.").replace("_", " "), status
                )
            return status
        finally:
            self.browser.guard.revoke_writes()

    def fact(self, field: str) -> str:
        value = get_fact(self.facts, field)
        if not isinstance(value, str) or not value:
            raise stop(
                "source_fact_missing",
                "A required supplied fact is missing.",
                "Provide the required source value.",
                field,
            )
        return value

    def dismiss_cookie(self) -> None:
        button = self.page.get_by_role("button", name=self.settings.labels["cookie_dismiss"], exact=True)
        if button.count() == 1 and button.is_visible():
            self.click(button, "portal.cookie_notice")

    def history(self) -> History:
        return RequestHistoryReader(self.browser, self.guard.settings.history, self.log).read(
            self.facts, self.guard.journal.case_key, self.request_id
        )

    def start(self) -> None:
        self.dismiss_cookie()
        self.guard.before_create(self.history())
        self.browser.guard.permit_write(self.base, "PUT", self.path("wizard"), self.settings.max_wizard_writes)
        self.click(self.page.get_by_role("link", name=self.settings.labels["new_request"], exact=True), "portal.intake")
        self.page.get_by_role("heading", name=self.settings.labels["new_request"], exact=True).wait_for(
            timeout=self.browser.timeout()
        )
        self.dismiss_cookie()
        draft = self.read("wizard").get("draft")
        if not isinstance(draft, dict) or any(draft.get(key) for key in ("patient", "drug", "form")):
            raise stop(
                "intake_already_started",
                "The portal has an existing intake selection.",
                "Review the existing intake before starting a new request.",
                "portal.request",
            )

    def select_patient(self) -> None:
        search = self.page.get_by_role("combobox", name=self.settings.labels["patient_search"], exact=True)
        self.fill(search, self.fact("patient.member_id"), "patient.member_id")
        region = self.page.get_by_role("region", name=self.settings.labels["patient_region"], exact=True)
        options = region.get_by_role("listbox", name="Patients", exact=True).get_by_role("option")
        empty = region.get_by_text(self.settings.labels["patient_no_matches"], exact=True)
        options.or_(empty).first.wait_for(timeout=self.browser.timeout())
        if options.count() == 0 and any(item.is_visible() for item in empty.all()):
            self.enter_patient(region)
            return
        if options.count() != 1:
            raise stop(
                "patient_match_ambiguous",
                "The patient search does not have one exact match.",
                "Identify the correct patient manually.",
                "patient.member_id",
            )
        text = normalize(options.inner_text())
        for field in (
            "patient.first_name",
            "patient.last_name",
            "patient.member_id",
            "patient.date_of_birth",
            "patient.state",
        ):
            if normalize(self.fact(field)) not in text:
                raise stop(
                    "patient_match_mismatch",
                    "The portal patient differs from the supplied record.",
                    "Review the patient identity without changing the source record.",
                    field,
                )
        self.click(options, "portal.patient_selection")
        self.verify_selected_patient()

    def enter_patient(self, region: Locator) -> None:
        self.click(
            region.get_by_role("button", name=self.settings.labels["new_patient"], exact=True),
            "portal.new_patient",
        )
        for field, label in self.settings.new_patient_fields.items():
            self.fill(region.get_by_label(label, exact=True), self.fact(field), field)
        for label, field, value in (
            (self.settings.labels["gender"], "patient.sex", self.settings.sex_labels.get(self.fact("patient.sex"))),
            (self.settings.labels["state"], "patient.state", self.fact("patient.state")),
        ):
            if not value:
                raise stop(
                    "patient_value_unsupported",
                    "No reviewed portal value matches the supplied fact.",
                    "Review this patient field without changing the source record.",
                    field,
                )
            control = region.get_by_role("button", name=label, exact=True)
            self.click(control, field)
            option_name = (
                re.compile(self.settings.state_option_pattern.format(value=re.escape(value)))
                if field == "patient.state"
                else value
            )
            option = self.page.get_by_role("option", name=option_name, exact=True)
            self.page.get_by_role("option").first.wait_for(timeout=self.browser.timeout())
            self.click(option, field)
            expect(control).to_contain_text(value, timeout=self.browser.timeout())
        self.verify_selected_patient()
        self.log.event("new_patient_verified")

    def verify_selected_patient(self) -> None:
        mismatch = "portal.patient"
        for attempt in range(10):
            draft = self.read("wizard").get("draft")
            selected = None
            if isinstance(draft, dict):
                selected = draft.get("new_patient") if draft.get("patient_mode") == "new" else draft.get("patient")
            if isinstance(selected, dict):
                mismatch = ""
                for key in ("first_name", "last_name", "date_of_birth", "member_id", "state", "zip_code", "gender"):
                    field = "patient.sex" if key == "gender" else "patient." + key
                    expected = self.settings.sex_labels.get(self.fact(field)) if key == "gender" else self.fact(field)
                    if selected.get(key) != expected:
                        mismatch = field
                        break
                if not mismatch:
                    break
            if attempt == 9:
                raise stop(
                    "patient_selection_mismatch",
                    "An intake patient field is missing or differs from the source record.",
                    "Review the intake patient before Create; do not change the source facts to fit.",
                    mismatch,
                )
            self.page.wait_for_timeout(min(200, self.browser.timeout()))
        self.fields.update(
            {
                "patient." + key: get_fact(self.facts, "patient." + key)
                for key in ("first_name", "last_name", "date_of_birth", "member_id", "sex", "state", "zip_code")
            }
        )
        self.log.event("intake_patient_verified")

    def select_product(self) -> None:
        search = self.page.get_by_role("combobox", name=self.settings.labels["medication_search"], exact=True)
        self.fill(search, self.fact("medication.name"), "medication.name")
        region = self.page.get_by_role("region", name=self.settings.labels["medication_region"], exact=True)
        # This portal may select a unique result itself. Otherwise inspect every visible option.
        self.page.wait_for_function(
            """() => [...document.querySelectorAll('[role=option],main strong')].some(e=>e.getClientRects().length)""",
            timeout=self.browser.timeout(),
        )
        options = region.get_by_role("option")
        if options.count():
            matching = options.filter(has_text=self.fact("medication.name")).filter(
                has_text=self.fact("medication.package")
            )
            if matching.count() != 1:
                raise stop(
                    "product_match_ambiguous",
                    "The medication and package do not identify one product.",
                    "Clarify the exact product and package.",
                    "medication.package",
                )
            self.click(matching, "portal.product_selection")
        region.get_by_text(self.fact("medication.name"), exact=True).wait_for(timeout=self.browser.timeout())
        wizard = self.read("wizard")
        draft = wizard.get("draft")
        drug = draft.get("drug") if isinstance(draft, dict) else None
        if (
            not isinstance(drug, dict)
            or drug.get("label") != self.fact("medication.name")
            or drug.get("package") != self.fact("medication.package")
        ):
            raise stop(
                "product_match_mismatch",
                "The selected product or package differs from the supplied record.",
                "Select the exact product manually.",
                "medication.package",
            )
        self.fields.update({field: get_fact(self.facts, field) for field in ("medication.name", "medication.package")})

    def select_plan(self) -> None:
        for label, field in (("BIN", "coverage.bin"), ("PCN", "coverage.pcn"), ("Group", "coverage.group")):
            self.fill(self.page.get_by_label(label, exact=True), self.fact(field), field)
        self.click(
            self.page.get_by_role("button", name=self.settings.labels["find_forms"], exact=True), "portal.plan_search"
        )
        self.page.get_by_role("radiogroup", name="Forms", exact=True).wait_for(timeout=self.browser.timeout())
        draft = self.read("wizard").get("draft")
        found = draft.get("found") if isinstance(draft, dict) else None
        plans = found.get("plans") if isinstance(found, dict) else None
        forms = found.get("forms") if isinstance(found, dict) else None
        if (
            not isinstance(plans, list)
            or len(plans) != 1
            or not isinstance(plans[0], dict)
            or not isinstance(forms, list)
        ):
            raise stop(
                "plan_match_ambiguous",
                "The coverage identifiers do not identify one plan.",
                "Verify BIN, PCN and group with the clinical staff.",
                "coverage.bin",
            )
        plan = plans[0]
        for key in ("bin", "pcn", "group"):
            if plan.get(key) != self.fact("coverage." + key):
                raise stop(
                    "plan_match_mismatch",
                    "The portal's plan identifiers differ from the supplied record.",
                    "Review the coverage identifiers.",
                    "coverage." + key,
                )
        coverage = self.facts.get("coverage")
        supplied_name = coverage.get("plan_name") if isinstance(coverage, dict) else None
        if supplied_name not in (None, "", plan.get("name")):
            raise stop(
                "plan_name_mismatch",
                "The supplied plan name conflicts with the matched identifiers.",
                "Clarify the patient's coverage.",
                "coverage.plan_name",
            )
        candidates = [
            item
            for item in forms
            if isinstance(item, dict)
            and isinstance(item.get("electronic"), bool)
            and item.get("plan_id") == plan.get("plan_id")
            and item.get("plan") == plan.get("name")
        ]
        if len(candidates) != 1 or not isinstance(candidates[0].get("title"), str):
            raise stop(
                "form_match_ambiguous",
                "The plan does not have one exact supported form.",
                "Select and review the appropriate form manually.",
                "coverage.bin",
            )
        form = candidates[0]
        self.electronic = form["electronic"]
        self.click(
            self.page.get_by_role("radio", name=re.compile("^" + re.escape(form["title"]) + r"(?:\s|$)")),
            "portal.form_selection",
        )
        self.fields.update(
            {field: get_fact(self.facts, field) for field in ("coverage.bin", "coverage.pcn", "coverage.group")}
        )

    def create(self) -> None:
        self.verify_selected_patient()
        self.browser.observe()
        self.write(
            self.page.get_by_role("button", name=self.settings.labels["create"], exact=True),
            "portal.create",
            "create",
            ("POST",),
        )
        self.page.wait_for_url(re.compile(r"/requests/[A-Za-z0-9-]+(?:[?#].*)?$"), timeout=self.browser.timeout())
        key = urlsplit(self.page.url).path.rsplit("/", 1)[1]
        if origin(self.page.url) != origin(self.base) or re.fullmatch(r"[A-Za-z0-9-]+", key) is None:
            raise stop(
                "creation_unverified",
                "The created request could not be identified.",
                "Inspect portal history; do not repeat Create.",
                "portal.request",
            )
        self.request_id = key
        self.guard.created(key)
        self.browser.guard.revoke_writes()
        self.verify_identity()

    def verify_identity(self) -> None:
        detail = self.read("detail").get("request")
        if not isinstance(detail, dict) or detail.get("key") != self.request_id or detail.get("status") != "Draft":
            raise stop(
                "draft_unverified",
                "The portal did not return this run's editable draft.",
                "Inspect the request before continuing.",
                "portal.request",
            )
        if detail.get("electronic") is not self.electronic:
            raise stop(
                "submission_route_changed",
                "The draft submission route differs from the selected form.",
                "Review the matched form and draft before sending.",
                "portal.request",
            )
        values = {
            "patient.member_id": detail.get("member_id"),
            "patient.date_of_birth": detail.get("date_of_birth"),
            "medication.name": detail.get("drug"),
            "medication.package": detail.get("drug_package"),
        }
        patient = detail.get("patient_detail")
        plan = detail.get("plan_detail")
        if not isinstance(patient, dict) or not isinstance(plan, dict):
            raise stop(
                "draft_identity_missing",
                "The draft identity cannot be verified.",
                "Review the draft.",
                "portal.request",
            )
        for key in ("first_name", "last_name", "state", "zip_code"):
            values["patient." + key] = patient.get(key)
        values["patient.sex"] = (
            self.fact("patient.sex")
            if patient.get("gender") == self.settings.sex_labels.get(self.fact("patient.sex"))
            else None
        )
        for key in ("bin", "pcn", "group"):
            values["coverage." + key] = plan.get(key)
        for field, value in values.items():
            if value != get_fact(self.facts, field):
                raise stop(
                    "draft_identity_mismatch",
                    "The draft differs from a supplied source fact.",
                    "Review the draft and the source record; do not change the source to fit.",
                    field,
                )
        self.fields.update(values)
        self.log.event("draft_identity_verified")

    def question_plan(self) -> AnswerPlan:
        payload = self.read("questions")
        rounds = payload.get("rounds")
        if not isinstance(rounds, list) or len(rounds) != 1 or not isinstance(rounds[0], dict):
            raise stop(
                "question_round_unsupported",
                "The request does not have one supported initial question round.",
                "Review the plan's current questions.",
                "portal.questions",
            )
        schema = rounds[0].get("set")
        questions = schema.get("questions") if isinstance(schema, dict) else None
        if not isinstance(questions, list) or not all(isinstance(item, dict) for item in questions):
            raise stop(
                "question_contract_unknown",
                "Question meanings cannot be inspected.",
                "Review the plan's form.",
                "portal.questions",
            )
        return plan_answers(questions, self.facts, self.settings)

    def answer_questions(self) -> AnswerPlan:
        self.click(self.page.get_by_role("tab", name=self.settings.labels["questions"], exact=True), "portal.questions")
        plan = self.question_plan()
        for question in plan.questions:
            answer = plan.answers[question.id]
            if question.kind == "radio":
                group = self.page.get_by_role("radiogroup", name=question.text, exact=True)
                group.wait_for(timeout=self.browser.timeout())
                if not answer:
                    if group.get_by_role("radio", checked=True).count():
                        raise stop(
                            "unsupported_default",
                            "An unsupported optional answer is preselected.",
                            "Review the default answer.",
                            "portal.questions",
                        )
                else:
                    control = group.get_by_role("radio", name=answer, exact=True)
                    self.one(control, "portal.questions")
                    self.click(group.get_by_text(answer, exact=True), "portal.questions")
                    expect(control).to_be_checked(timeout=self.browser.timeout())
            else:
                control = self.page.get_by_role("textbox", name=question.text, exact=True)
                control.wait_for(timeout=self.browser.timeout())
                self.fill(
                    control,
                    answer,
                    self.settings.rule(question.text, question.kind).source
                    if self.settings.rule(question.text, question.kind)
                    else "portal.questions",
                )
            self.log.event("question_answer_verified", observation=digest(question.text))
        self.write(
            self.page.get_by_role("button", name=self.settings.labels["save"], exact=True),
            "portal.save_answers",
            "answers",
            ("PUT", "POST"),
        )
        return self.verify_saved_answers(plan)

    def verify_saved_answers(self, plan: AnswerPlan) -> AnswerPlan:
        # The portal stores dates as ISO, while its calendar textbox displays MM/DD/YYYY.
        for _attempt in range(10):
            saved = self.read("questions")
            stored_rounds = saved.get("rounds")
            stored = stored_rounds[0] if isinstance(stored_rounds, list) and len(stored_rounds) == 1 else None
            if isinstance(stored, dict) and stored.get("saved_at"):
                schema = stored.get("set")
                current = schema.get("questions") if isinstance(schema, dict) else None
                if digest(current) != plan.schema_digest:
                    raise stop(
                        "question_schema_changed",
                        "The plan's questions changed after review.",
                        "Review the current questionnaire before sending.",
                        "portal.questions",
                    )
                answers = stored.get("answers")
                if not isinstance(answers, dict) or any(
                    answers.get(key, "") != value for key, value in plan.stored_answers.items()
                ):
                    raise stop(
                        "saved_answers_mismatch",
                        "The portal's saved answers differ from the reviewed source facts.",
                        "Review the saved answers before sending.",
                        "portal.questions",
                    )
                if any(
                    key not in plan.answers and value not in ("", None) for key, value in answers.items()
                ) or stored.get("missing"):
                    raise stop(
                        "saved_answers_incomplete",
                        "The saved form has unsupported or missing answers.",
                        "Review conditional fields and required answers.",
                        "portal.questions",
                    )
                self.fields.update(plan.fields)
                self.answer_plan = plan
                self.browser.guard.revoke_writes()
                self.log.event("saved_answers_verified")
                return plan
            self.page.wait_for_timeout(min(200, self.browser.timeout()))
        raise stop(
            "answer_save_unverified",
            "The portal did not confirm that answers were saved.",
            "Inspect the draft before sending.",
            "portal.questions",
        )

    def send(self) -> RunResult:
        self.verify_identity()
        if self.answer_plan is None:
            raise stop(
                "answers_unverified",
                "No verified answer plan is available.",
                "Review the saved answers.",
                "portal.questions",
            )
        self.verify_saved_answers(self.answer_plan)
        history = self.history()
        self.page.goto(self.base + "/requests/" + (self.request_id or ""), timeout=self.browser.timeout())
        labels = set(self.settings.submission_labels.values())
        open_pattern = re.compile("^(?:" + "|".join(re.escape(label) for label in sorted(labels)) + ")$")
        open_button = self.page.get_by_role("button", name=open_pattern)
        open_button.wait_for(timeout=self.browser.timeout())
        self.click(open_button, "portal.open_attestation")
        dialog = self.page.get_by_role("dialog").filter(
            has=self.page.get_by_role("textbox", name=re.compile("name", re.I))
        )
        dialog.wait_for(timeout=self.browser.timeout())
        warning = dialog.get_by_text(self.settings.labels["missing_questions"], exact=True)
        if warning.count() == 1 and warning.is_visible():
            questions = warning.locator("xpath=..").get_by_role("listitem").all_inner_texts()
            raise stop(
                "portal_questions_missing",
                "Required questions remain unanswered: " + "; ".join(questions),
                "Clinical staff must provide the listed facts before attesting or sending.",
                "portal.questions",
            )
        if self.electronic is None:
            raise stop(
                "submission_route_unverified",
                "The request's submission route is unverified.",
                "Review the matched form's submission route.",
                "portal.request",
            )
        route_label = self.settings.submission_labels["electronic" if self.electronic else "fax"]
        confirm = dialog.get_by_role("button", name=route_label, exact=True)
        if confirm.count() != 1 or not confirm.is_visible():
            raise stop(
                "submission_route_unverified",
                "The dialog does not expose the verified submission route.",
                "Review the plan's electronic or fax route.",
                "portal.submission",
            )
        statement = dialog.get_by_text(self.settings.attestation_statement, exact=True)
        if statement.count() != 1 or not statement.is_visible():
            raise stop(
                "attestation_unreviewed",
                "The portal's attestation statement is unsupported.",
                "Review the attestation before signing.",
                "portal.attestation",
            )
        name = dialog.get_by_role("textbox", name=re.compile("name", re.I))
        self.fill(name, self.fact("attestation_name"), "attestation_name")
        self.fields["attestation_name"] = self.fact("attestation_name")
        self.one(confirm, "portal.submission")
        self.browser.observe()
        self.log.event("submission_safety_verified")
        self.guard.before_send(history, self.fields)
        status = self.write(confirm, "portal.send", "send", ("POST",), check_validation=False)
        # Page-response bodies can remain unfinished after navigation. Read bounded request state instead.
        detail = self.read("detail").get("request")
        if isinstance(detail, dict) and detail.get("key") == self.request_id and detail.get("status") == "FAILED":
            self.page.get_by_role(
                "heading", name=re.compile("^" + re.escape(self.request_id or "") + r"\s+FAILED$")
            ).wait_for(timeout=self.browser.timeout())
            self.browser.observe()
            if re.search(self.guard.settings.history.receipt_pattern, self.page.locator("main").inner_text()):
                raise stop(
                    "outcome_conflict",
                    "A rejection and a submission receipt are both visible.",
                    "Inspect the request before deciding its outcome; do not send again.",
                    "portal.request",
                )
            result = RunResult(
                status="FAILED",
                reason="The portal explicitly rejected the validated submission.",
                fields_filled=self.fields,
            )
            self.guard.finish(result, request_id=self.request_id or "")
            return result
        if status in (400, 422):
            raise validation_rejection(self.browser, self.settings, "Send", status)
        self.page.get_by_role(
            "heading", name=re.compile("^" + re.escape(self.request_id or "") + r"\s+Sent to plan$")
        ).wait_for(timeout=self.browser.timeout())
        self.browser.observe()
        main = self.page.locator("main")
        receipts = set(re.findall(self.guard.settings.history.receipt_pattern, main.inner_text()))
        if len(receipts) != 1:
            raise stop(
                "receipt_unverified",
                "The sent request does not show one confirmation number.",
                "Inspect the saved request; do not send again.",
                "portal.request",
            )
        result = RunResult(
            status="SUCCESS",
            reason="The portal verified that the request was sent to the plan.",
            confirmation_id=receipts.pop(),
            fields_filled=self.fields,
        )
        self.guard.finish(result, request_id=self.request_id or "")
        return result

    def resume(self, request_id: str, history: History) -> RunResult:
        # Operator-selected continuation of the journal's draft; never Create or retry Send.
        self.guard.authorize_draft_resume(history, request_id)
        self.request_id = request_id
        detail = self.read("detail").get("request")
        route = detail.get("electronic") if isinstance(detail, dict) else None
        if not isinstance(route, bool):
            raise stop(
                "submission_route_unverified",
                "The saved draft's submission route is unavailable.",
                "Review the existing request manually.",
                "portal.request",
            )
        self.electronic = route
        self.verify_identity()
        self.verify_saved_answers(self.question_plan())
        return self.send()

    def run(self) -> RunResult:
        self.start()
        self.select_patient()
        self.select_product()
        self.select_plan()
        self.create()
        self.answer_questions()
        return self.send()
