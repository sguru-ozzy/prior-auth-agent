"""Complete UI history scans with source identity checks and explicit pagination evidence."""

import re
import time
from datetime import date
from typing import NoReturn

from prior_auth_agent.agent.proposals import digest, normalize, stop
from prior_auth_agent.agent.recovery import History, RecoveryGuard, RequestMatch
from prior_auth_agent.semantic.bindings import BindingResolver
from prior_auth_agent.semantic.browser import SemanticBrowser
from prior_auth_agent.semantic.contracts import SemanticSettings, UIAction, UIControl, UIRow, UISnapshot, matches
from prior_auth_agent.validation import get_fact


def history_stop(problem: str) -> NoReturn:
    raise stop(
        "history_unverified",
        problem,
        "Review the full request history manually; do not create or resend a request.",
        "portal.requests",
    )


def unique_control(snapshot: UISnapshot, labels: list[str], *, enabled: bool = True) -> UIControl | None:
    controls = [
        c
        for c in snapshot.controls
        if c.role in ("button", "link", "tab") and matches(c.label, labels) and (c.enabled or not enabled)
    ]
    if (
        len(controls) > 1
        and all(c.role == "link" and c.href for c in controls)
        and len({c.href for c in controls}) == 1
    ):
        controls = controls[:1]
    if len(controls) > 1:
        history_stop("A required history navigation control is ambiguous.")
    return controls[0] if controls else None


def detail_value(
    snapshot: UISnapshot, settings: SemanticSettings, field: str, bindings: BindingResolver | None = None
) -> str:
    values = bindings.values(snapshot, field) if bindings else snapshot.values(settings.detail_labels.get(field, []))
    if len(values) != 1:
        history_stop("A required request detail is missing or ambiguous: " + field)
    return values[0]


def same_value(actual: str, expected: str, field: str) -> bool:
    if field.endswith(("date_of_birth", "start_date")):
        try:
            expected_date = date.fromisoformat(expected)
        except ValueError:
            return False
        return actual in (expected, expected_date.strftime("%m/%d/%Y"), expected_date.strftime("%m/%d/%Y").lstrip("0"))
    if field in ("patient.member_id", "coverage.bin", "coverage.pcn", "coverage.group", "prescriber.npi"):
        return actual == expected
    return normalize(actual) == normalize(expected)


def request_id(snapshot: UISnapshot, settings: SemanticSettings) -> str:
    values = snapshot.values(settings.detail_labels["request_id"])
    values.extend(
        match.group("id") for pattern in settings.request_id_patterns for match in re.finditer(pattern, snapshot.text)
    )
    values = list(dict.fromkeys(values))
    if len(values) != 1 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,100}", values[0]) is None:
        history_stop("The request does not expose one explicit request identifier.")
    return values[0]


def receipt(snapshot: UISnapshot, settings: SemanticSettings) -> str | None:
    values = snapshot.values(settings.detail_labels["confirmation_id"])
    for pattern in settings.reference_patterns:
        values.extend(match.group("id") for match in re.finditer(pattern, snapshot.text))
    values = list(dict.fromkeys(values))
    if len(values) > 1:
        history_stop("The request shows more than one confirmation identifier.")
    if values and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,100}", values[0]) is None:
        history_stop("The confirmation identifier has an unsupported format.")
    return values[0] if values else None


class SemanticHistoryReader:
    def __init__(
        self,
        browser: SemanticBrowser,
        guard: RecoveryGuard,
        settings: SemanticSettings,
        bindings: BindingResolver | None = None,
    ) -> None:
        self.ui = browser
        self.guard = guard
        self.settings = settings
        self.bindings = bindings
        self.seen_ids: set[str] = set()

    def _fact(self, field: str) -> str:
        value = get_fact(self.guard.facts, field)
        if not isinstance(value, str):
            history_stop("The supplied request identity is incomplete.")
        return value

    def _bounds(self, snapshot: UISnapshot) -> tuple[int, int, int]:
        results = {
            (int(m.group("first")), int(m.group("last")), int(m.group("total")))
            for pattern in self.settings.pagination_patterns
            for m in re.finditer(pattern, snapshot.text)
        }
        if len(results) == 1:
            first, last, total = next(iter(results))
            if 0 <= first <= last <= total and (total == 0 or first >= 1):
                return first, last, total
        if (
            not results
            and not any(table.rows for table in snapshot.tables)
            and any(matches(line, self.settings.empty_history) for line in snapshot.text.splitlines())
        ):
            return 0, 0, 0
        history_stop("The history does not prove its total count and page boundaries.")
        raise AssertionError

    def _column(self, label: str, field: str) -> bool:
        if matches(label, self.settings.history_columns[field]):
            return True
        return self.bindings is not None and self.bindings.label_matches(label, "Request history", field)

    def _row(self, snapshot: UISnapshot, row: UIRow, headers: list[str]) -> tuple[str, str, str, str]:
        values: dict[str, str] = {}
        if headers:
            if len(headers) != len(row.cells):
                history_stop("A history row does not match its visible columns.")
            for field in self.settings.history_columns:
                indices = [index for index, label in enumerate(headers) if self._column(label, field)]
                if len(indices) != 1:
                    history_stop("A history column has an unsupported or ambiguous meaning: " + field)
                values[field] = row.cells[indices[0]]
        else:
            for field in self.settings.history_columns:
                candidates = [p.value for p in row.pairs if self._column(p.label, field)]
                if len(set(candidates)) != 1:
                    history_stop("A request card has incomplete labeled details: " + field)
                values[field] = candidates[0]
        key = values["request_id"]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,100}", key) is None:
            history_stop("A history row has no supported request identifier.")
        controls = [snapshot.control(ref) for ref in row.controls]
        links = [c for c in controls if c.href and c.label == key]
        if not links:
            links = [c for c in controls if c.href and matches(c.label, self.settings.intents.get("view_request", []))]
        if not links:
            links = [c for c in controls if c.href]
        if len(links) != 1:
            history_stop("A request row does not identify one detail link.")
        return key, values["medication.name"], values["status"], links[0].href

    def read(self) -> History:
        initial = self.ui.stable()
        navigation = unique_control(initial, self.settings.intents["history"])
        if navigation is None:
            history_stop("The portal does not expose supported request-history navigation.")
        snapshot = self.ui.perform(
            initial, UIAction(intent="history", target=navigation.ref), write=True, read_operation=True
        )
        reset = unique_control(snapshot, self.settings.intents["clear_filters"])
        if reset is not None:
            snapshot = self.ui.perform(
                snapshot, UIAction(intent="clear_filters", target=reset.ref), write=True, read_operation=True
            )
        for control in snapshot.controls:
            if (
                control.role in ("textbox", "combobox", "spinbutton")
                and control.value
                and not matches(control.label, self.settings.non_filter_labels)
                and normalize(control.value) not in {normalize(value) for value in self.settings.clear_filter_values}
            ):
                history_stop("Request-history filters are not verifiably clear.")
            if (
                control.checked
                and (
                    control.role in ("checkbox", "radio")
                    or matches(control.label, self.settings.open_statuses + self.settings.closed_statuses)
                )
                and not matches(control.label, self.settings.all_history_labels)
            ):
                history_stop("A request-status filter remains active.")
        matches_found: list[RequestMatch] = []
        seen: set[str] = set()
        candidates: list[tuple[str, str, str]] = []
        expected_total: int | None = None
        for _ in range(self.settings.max_history_pages):
            first, last, total = self._bounds(snapshot)
            if total > self.settings.max_history_rows or (expected_total is not None and total != expected_total):
                history_stop("History changed or exceeds the bounded scan size.")
            expected_total = total
            if total == 0:
                break
            tables = [
                table
                for table in snapshot.tables
                if table.rows
                and (
                    all(
                        any(self._column(header, field) for header in table.headers)
                        for field in self.settings.history_columns
                    )
                    if table.headers
                    else all(
                        all(
                            any(self._column(pair.label, field) for pair in row.pairs)
                            for field in self.settings.history_columns
                        )
                        for row in table.rows
                    )
                )
            ]
            if len(tables) != 1 or first != len(seen) + 1 or len(tables[0].rows) != last - first + 1:
                history_stop("The history layout or pagination does not prove a complete scan.")
            for row in tables[0].rows:
                key, drug, status, link = self._row(snapshot, row, tables[0].headers)
                if key in seen:
                    history_stop("A request identifier is repeated across history pages.")
                seen.add(key)
                if re.search(
                    r"(?<![\w])" + re.escape(normalize(self._fact("medication.name"))) + r"(?![\w])", normalize(drug)
                ):
                    candidates.append((key, status, link))
            self.ui.log.event("semantic_history_page_verified", observation=digest(sorted(seen)))
            if last == total:
                break
            next_page = unique_control(snapshot, self.settings.intents["next_page"])
            if next_page is None:
                history_stop("The remaining request pages cannot be inspected.")
            snapshot = self.ui.perform(
                snapshot, UIAction(intent="next_page", target=next_page.ref), write=True, read_operation=True
            )
        if expected_total is None or len(seen) != expected_total:
            history_stop("Not every request in the history was inspected.")
        self.seen_ids = seen.copy()
        for key, status, link in candidates:
            detail = self.ui.navigate(link)
            if request_id(detail, self.settings) != key:
                history_stop("The request detail identifier differs from its history row.")
            if not same_value(
                detail_value(detail, self.settings, "patient.member_id", self.bindings),
                self._fact("patient.member_id"),
                "patient.member_id",
            ):
                if key == self.guard.journal.request_id:
                    history_stop("The saved request belongs to a different patient.")
                continue
            for field in ("patient.date_of_birth", "medication.name", "medication.package"):
                if not same_value(detail_value(detail, self.settings, field, self.bindings), self._fact(field), field):
                    history_stop("A matching request has conflicting identity or medication details: " + field)
            actual_status = detail_value(detail, self.settings, "status")
            if normalize(actual_status) != normalize(status):
                history_stop("The request status changed during the history scan.")
            state = (
                "open"
                if matches(status, self.settings.open_statuses)
                else ("closed" if matches(status, self.settings.closed_statuses) else "unknown")
            )
            confirmation = receipt(detail, self.settings)
            submitted = matches(status, self.settings.submitted_statuses)
            if submitted and confirmation is None:
                history_stop("A submitted request has no verifiable confirmation.")
            matches_found.append(
                RequestMatch(request_id=key, state=state, submitted=submitted, confirmation_id=confirmation)
            )
        return History(
            case_key=self.guard.journal.case_key,
            record_digest=self.guard.journal.record_digest,
            requests=matches_found,
            complete=True,
            checked_at=time.monotonic(),
        )
