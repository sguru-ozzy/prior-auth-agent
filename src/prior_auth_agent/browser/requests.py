"""Reviewed read-only request history; incomplete evidence never clears a duplicate check."""

import re
import time
from urllib.parse import urljoin

from playwright.sync_api import Locator, expect
from playwright.sync_api import TimeoutError as BrowserTimeout
from pydantic import JsonValue

from prior_auth_agent.agent.proposals import digest, normalize
from prior_auth_agent.agent.recovery import History, HistoryRules, RequestMatch
from prior_auth_agent.browser.session import PortalBrowser, origin
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.validation import get_fact


def history_problem(problem: str, field: str = "portal.requests") -> InputProblem:
    return InputProblem(
        Issue(
            field,
            "history_unverified",
            problem,
            "Review the request history and matching request details manually; do not create or resend a request.",
        )
    )


class RequestHistoryReader:
    def __init__(self, browser: PortalBrowser, rules: HistoryRules, log: ActionLog | None = None) -> None:
        self.browser = browser
        self.page = browser.page
        self.rules = rules
        self.log = log
        self.summary_pattern = re.compile(rules.summary_pattern)
        self.throttle_pattern = re.compile(rules.throttle_pattern)
        self.read_error_pattern = re.compile(
            "(?:" + rules.read_error_pattern + ")|(?:" + re.sub(r"\(\?P<[^>]+>", "(", rules.throttle_pattern) + ")"
        )
        # Playwright runs regexes in JavaScript, which does not accept Python's named-group syntax.
        self.summary_text_pattern = re.compile(re.sub(r"\(\?P<[^>]+>", "(", rules.summary_pattern))

    def _event(self, name: str, *, observation: str | None = None, attempt: int | None = None) -> None:
        if self.log is not None:
            self.log.event(name, observation=observation, attempt=attempt)

    def _wait_read(self, expected: Locator) -> None:
        error = self.page.get_by_role("alert").filter(has_text=self.read_error_pattern)
        for attempt in range(self.rules.max_read_retries + 1):
            expected.or_(error).first.wait_for(timeout=self.browser.read_timeout())
            if error.count() == 0 and expected.count() and expected.first.is_visible():
                return
            if error.count() != 1 or not error.is_visible() or attempt == self.rules.max_read_retries:
                break
            throttle = self.throttle_pattern.fullmatch(error.inner_text().strip())
            label = self.rules.throttle_retry_button if throttle else self.rules.read_retry_button
            button = self.page.get_by_role("button", name=label, exact=True)
            if button.count() != 1 or not button.is_visible():
                break
            # This reader has no Create/Send grants; recovery can only issue trusted reads.
            self.browser.observe()
            headers = (
                {"retry-after": throttle.group("seconds")}
                if throttle
                else self.browser.read_error_headers.pop(self.rules.read_endpoint, {})
            )
            self.browser.reads.pause(attempt, headers, "ui_throttled" if throttle else "ui_read_error")
            self._event("history_read_retry", attempt=attempt + 1)
            # The countdown may refresh the page automatically while we honor its delay.
            if button.count() == 1 and button.is_visible():
                button.click(timeout=self.browser.timeout())
        raise history_problem("A required portal view could not be loaded within its bounded read retry limit.")

    def _wait_page(self, expected_first: int | None = None) -> None:
        summary = self.page.get_by_text(self.summary_text_pattern)
        if expected_first is not None:
            summary = summary.filter(has_text=re.compile(r"^Showing\s+" + str(expected_first) + r"[–-]"))
        self._wait_read(summary)

    def _summary(self) -> tuple[int, int, int]:
        self._wait_page()
        summary = self.page.get_by_text(self.summary_text_pattern)
        if summary.count() != 1:
            summary.first.wait_for(timeout=self.browser.read_timeout())
        if summary.count() != 1:
            raise history_problem("The request count or pagination summary is ambiguous.")
        match = self.summary_pattern.fullmatch(summary.inner_text().strip())
        if match is None:
            raise history_problem("The request count could not be verified.")
        return tuple(int(match.group(name)) for name in ("first", "last", "total"))

    def _term(self, label: str) -> str:
        term = self.page.locator("main dt").filter(has_text=re.compile("^" + re.escape(label) + "$"))
        if term.count() <= 1:
            try:
                self._wait_read(term)
            except BrowserTimeout:
                self.browser.observe()
                raise history_problem(f"The request detail field {label!r} did not become available.") from None
        if term.count() != 1 or not term.is_visible():
            raise history_problem("A required request-detail field is missing or ambiguous.")
        value = term.locator("xpath=following-sibling::dd[1]")
        if value.count() <= 1:
            try:
                self._wait_read(value)
            except BrowserTimeout:
                self.browser.observe()
                raise history_problem(f"The request detail value for {label!r} did not become available.") from None
        if value.count() != 1 or not value.is_visible():
            raise history_problem("A required request-detail value could not be verified.")
        return value.inner_text().strip()

    def read(self, facts: dict[str, JsonValue], case_key: str, request_id: str | None = None) -> History:
        started = time.monotonic()
        navigation = self.page.get_by_role("link", name=self.rules.navigation, exact=True)
        if navigation.count() != 1:
            raise history_problem("The reviewed request-history navigation is unavailable.")
        self._event("history_navigation_requested")
        navigation.click(timeout=self.browser.read_timeout())
        self.page.get_by_role("heading", name=self.rules.heading, exact=True).wait_for(
            timeout=self.browser.read_timeout()
        )
        clear = self.page.get_by_role("button", name=self.rules.clear_filters, exact=True)
        if clear.count() != 1:
            raise history_problem("Request filters cannot be cleared safely.")
        self._event("history_filter_reset_requested")
        clear.click(timeout=self.browser.read_timeout())
        for label in self.rules.filter_labels:
            field = self.page.get_by_label(label, exact=True)
            if field.count() != 1:
                raise history_problem("A reviewed request filter is missing or ambiguous.")
            expect(field).to_have_value("", timeout=self.browser.read_timeout())
        for status in self.rules.open_statuses + self.rules.closed_statuses:
            button = self.page.get_by_role("button", name=status, exact=True)
            if button.count() != 1 or button.get_attribute("aria-pressed") != "false":
                raise history_problem("The request-status filters are not verifiably clear.")

        self._event("history_filters_verified")
        self._wait_page()
        table = self.page.get_by_role("table")
        if table.count() != 1:
            raise history_problem("The request-history table is missing or ambiguous.")
        self.page.get_by_text(self.summary_text_pattern).wait_for(timeout=self.browser.read_timeout())
        headers = [normalize(text) for text in table.locator("thead th").all_text_contents()]
        indexes = {}
        for field, heading in self.rules.columns.items():
            if headers.count(normalize(heading)) != 1:
                raise history_problem("A reviewed request-history column is missing or ambiguous.")
            indexes[field] = headers.index(normalize(heading))

        target_drug = normalize(str(get_fact(facts, "medication.name")))
        candidates: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        expected_first = 1
        total_expected: int | None = None
        for page_index in range(self.rules.max_pages):
            first, last, total = self._summary()
            if total > self.rules.max_requests:
                raise history_problem("The request history exceeds the reviewed scan limit.")
            if total_expected is None:
                total_expected = total
            if total != total_expected or (total > 0 and (first != expected_first or not first <= last <= total)):
                raise history_problem("The request history changed or pagination is incomplete.")
            if total == 0:
                if first != 0 or last != 0 or table.locator("tbody a").count():
                    raise history_problem("An empty request history could not be verified.")
                break
            page_keys = []
            rows = table.locator("tbody tr")
            expect(rows).to_have_count(last - first + 1, timeout=self.browser.read_timeout())
            for row in rows.all():
                cells = row.locator("td")
                if cells.count() < len(headers):
                    raise history_problem("A request row is incomplete.")
                key_cell = cells.nth(indexes["key"])
                link = key_cell.get_by_role("link")
                if link.count() != 1:
                    raise history_problem("A request row does not identify a single request.")
                key = link.inner_text().strip()
                if not key or key in seen:
                    raise history_problem("A request ID is missing or repeated across pages.")
                seen.add(key)
                page_keys.append(key)
                drug = cells.nth(indexes["drug"]).inner_text().strip()
                status = cells.nth(indexes["status"]).inner_text().strip()
                if normalize(drug) == target_drug or key == request_id:
                    href = link.get_attribute("href")
                    if not href:
                        raise history_problem("A request detail link is missing.")
                    url = urljoin(self.page.url, href)
                    if origin(url) != origin(self.browser.start_url):
                        raise history_problem("A request detail link points outside the reviewed portal.")
                    candidates.append((key, url, status))
            self._event("history_page_verified", observation=digest(page_keys), attempt=page_index + 1)
            if last == total:
                break
            next_page = self.page.get_by_role("button", name=self.rules.next_button, exact=True)
            if next_page.count() != 1 or not next_page.is_enabled():
                raise history_problem("The remaining request pages cannot be inspected.")
            self._event("history_next_page_requested", attempt=page_index + 2)
            next_page.click(timeout=self.browser.read_timeout())
            expected_first = last + 1
            self._wait_page(expected_first)
        else:
            raise history_problem("The request history exceeds the reviewed page limit.")
        if len(seen) != total_expected:
            raise history_problem("Not every request in the history was inspected.")

        matches = []
        for key, url, list_status in candidates:
            self._event("history_request_read_requested", observation=digest(key))
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.browser.read_timeout())
            self._wait_read(self.page.get_by_role("heading", name=re.compile("^" + re.escape(key) + r"\b")))
            fields = self.rules.detail_fields
            member = self._term(fields["member_id"])
            if member != get_fact(facts, "patient.member_id"):
                if key == request_id:
                    raise history_problem("The saved request ID belongs to a different patient.", "recovery.state")
                continue
            if self._term(fields["date_of_birth"]) != get_fact(facts, "patient.date_of_birth"):
                raise history_problem("The portal DOB differs from the supplied patient DOB.", "patient.date_of_birth")
            drug = self._term(fields["drug"])
            if normalize(drug) != target_drug:
                raise history_problem(
                    "The request's medication differs from the supplied medication.", "medication.name"
                )
            package = self._term(fields["package"])
            if key == request_id and package != get_fact(facts, "medication.package"):
                raise history_problem(
                    "The saved request package differs from the supplied package.", "medication.package"
                )
            heading = self.page.get_by_role("heading", name=re.compile("^" + re.escape(key) + r"\b"))
            if heading.count() != 1:
                raise history_problem("The request status heading is ambiguous.")
            status = heading.inner_text().strip()[len(key) :].strip()
            if normalize(status) != normalize(list_status):
                raise history_problem("The request status changed during the history scan.")
            state = "unknown"
            if status in self.rules.open_statuses:
                state = "open"
            elif status in self.rules.closed_statuses:
                state = "closed"
            receipts = re.findall(self.rules.receipt_pattern, self.page.locator("main").inner_text())
            if len(set(receipts)) > 1:
                raise history_problem("More than one confirmation is visible for the matching request.")
            receipt = receipts[0] if receipts else None
            submitted = status in self.rules.submitted_statuses and receipt is not None
            matches.append(
                RequestMatch(
                    request_id=key,
                    state=state,
                    submitted=submitted,
                    confirmation_id=receipt,
                )
            )
        # No source identifiers or raw page content leave this adapter.
        self.browser.observe()
        return History(
            case_key=case_key, record_digest=digest(facts), requests=matches, complete=True, checked_at=started
        )
