"""Bounded UI actions and evidence reads; never calls a portal-specific endpoint."""

import json
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Locator, Response, expect

from prior_auth_agent.agent.proposals import digest, stop
from prior_auth_agent.browser.session import PortalBrowser, origin
from prior_auth_agent.semantic.contracts import SemanticSettings, UIAction, UIControl, UISnapshot
from prior_auth_agent.storage.audit import ActionLog

SEMANTIC_OBSERVER = files("prior_auth_agent.semantic").joinpath("observe.js").read_text(encoding="utf-8")


def contains_query(response: Response, value: str, allowed_origins: set[str]) -> bool:
    if origin(response.url) not in allowed_origins or response.request.method not in ("GET", "POST"):
        return False
    params = parse_qs(urlsplit(response.request.url).query)
    if any(value in values for values in params.values()):
        return True
    raw = response.request.post_data or ""
    if any(value in values for values in parse_qs(raw).values()):
        return True
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return isinstance(document, dict) and value in document.values()


class SemanticBrowser:
    def __init__(self, browser: PortalBrowser, settings: SemanticSettings, log: ActionLog) -> None:
        self.browser = browser
        self.page = browser.page
        self.settings = settings
        self.log = log
        self.actions = 0
        self.last_write_status: int | None = None
        self.executed: set[tuple[str, str]] = set()

    def snapshot(self) -> UISnapshot:
        if self.browser.guard.blocked or self.browser.unexpected_surface:
            raise stop(
                "unexpected_browser_surface",
                "An unapproved request or browser surface interrupted the workflow.",
                "Review the page and preserve the case journal before continuing.",
            )
        self.browser.timeout()
        self.browser.dismiss_known_overlays()
        if origin(self.page.url) not in self.browser.guard.origins:
            raise stop(
                "origin_changed", "The page left the trusted origins.", "Review the destination.", "portal.access"
            )
        raw = self.page.evaluate(
            SEMANTIC_OBSERVER, self.settings.model_dump(include={"max_controls", "max_text_length"})
        )
        snapshot = UISnapshot.model_validate(raw)
        if snapshot.truncated:
            raise stop(
                "observation_truncated", "The visible page exceeds the observation budget.", "Review the page manually."
            )
        self.log.event("semantic_page_observed", observation=snapshot.fingerprint)
        return snapshot

    def stable(self) -> UISnapshot:
        previous = self.snapshot()
        for _ in range(self.settings.max_unchanged + 1):
            self.page.wait_for_timeout(min(self.settings.settle_ms, self.browser.timeout()))
            current = self.snapshot()
            if not current.busy and current.fingerprint == previous.fingerprint:
                return current
            previous = current
        raise stop(
            "page_not_settled",
            "The page did not settle within the observation limit.",
            "Review loading or changing results.",
        )

    def target(self, snapshot: UISnapshot, ref: str) -> Locator:
        if self.snapshot().fingerprint != snapshot.fingerprint:
            raise stop(
                "stale_proposal", "The page changed after the action was proposed.", "Observe the current page again."
            )
        snapshot.control(ref)
        target = self.page.locator(f'[data-paa-ref="{ref}"]')
        if target.count() != 1 or not target.is_visible() or not target.is_enabled():
            raise stop(
                "target_ambiguous", "The target is missing, disabled or ambiguous.", "Review the current control."
            )
        return target

    def navigate(self, url: str) -> UISnapshot:
        if origin(url) not in self.browser.guard.origins:
            raise stop("origin_changed", "A history link leaves the trusted portal.", "Review the request link.")
        self.actions += 1
        if self.actions > self.settings.max_actions:
            raise stop("action_budget", "The browser action budget was reached.", "Review the current case.")
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.browser.read_timeout())
        self.log.event("semantic_read_navigation", observation=digest(url))
        return self.stable()

    def perform(
        self,
        snapshot: UISnapshot,
        action: UIAction,
        *,
        write: bool = False,
        search: bool = False,
        read_operation: bool = False,
    ) -> UISnapshot:
        key = (snapshot.fingerprint, action.id)
        if key in self.executed:
            raise stop(
                "action_repeated", "The same action would be repeated without a verified change.", "Review the page."
            )
        self.actions += 1
        if self.actions > self.settings.max_actions:
            raise stop("action_budget", "The browser action budget was reached.", "Review the current case.")
        control = snapshot.control(action.target)
        target = self.target(snapshot, action.target)
        if control.href and origin(control.href) not in self.browser.guard.origins:
            raise stop("origin_changed", "The proposed link leaves the trusted origins.", "Review the destination.")
        if write:
            # An observed HTML form narrows the grant further. JavaScript actions get one
            # same-page-origin mutation, only while this single code-authorized action runs.
            destination = control.form_action if control.form_method == "POST" else snapshot.url
            if origin(destination) not in self.browser.guard.origins:
                raise stop("origin_changed", "The form destination is untrusted.", "Review the form destination.")
            form_path = urlsplit(destination).path if control.form_method == "POST" else None
            self.browser.guard.permit_ui_action(destination, form_path, "POST" if form_path or read_operation else None)
        self.last_write_status = None
        responses: list[Response] = []

        def capture(response: Response) -> None:
            if response.request.method not in ("GET", "HEAD"):
                responses.append(response)

        before_writes = self.browser.guard.ui_writes
        self.page.on("response", capture)
        self.executed.add(key)
        self.log.event(
            "semantic_action_authorized", action=action.intent, binding=action.source, observation=snapshot.fingerprint
        )
        try:
            if search:
                if action.value is None:
                    raise stop("search_invalid", "The search has no supplied value.", "Review the search mapping.")
                with self.page.expect_response(
                    lambda response: contains_query(response, action.value or "", self.browser.guard.origins),
                    timeout=self.browser.read_timeout(),
                ) as pending:
                    self._execute(target, action, control)
                if not pending.value.ok:
                    raise stop(
                        "search_unavailable",
                        "The current search was rejected.",
                        "Review portal availability.",
                        action.source or "portal.search",
                    )
            else:
                self._execute(target, action, control)
            current = self.stable()
            if self.browser.guard.ui_writes > before_writes:
                request = self.browser.guard.ui_last_request
                observed = [response for response in responses if response.request == request]
                response = (
                    observed[0]
                    if observed
                    else self.page.wait_for_event(
                        "response",
                        predicate=lambda response: response.request == request,
                        timeout=self.browser.read_timeout(),
                    )
                )
                self.last_write_status = response.status
                if response.status in (400, 409, 422):
                    raise stop(
                        "portal_validation_rejected",
                        "The portal rejected this action or its supplied data.",
                        "Review the portal validation message; do not alter source values to make it pass.",
                        action.source or "portal.validation",
                    )
                if not response.ok:
                    raise stop(
                        "write_outcome_uncertain",
                        "The authorized write did not return a successful response.",
                        "Inspect the existing request and preserve its journal; do not repeat the write.",
                    )
                current = self.stable()
            self.log.event("semantic_action_verified", action=action.intent, binding=action.source)
            return current
        finally:
            self.page.remove_listener("response", capture)
            self.browser.guard.revoke_writes()

    def _execute(self, target: Locator, action: UIAction, control: UIControl) -> None:
        timeout = self.browser.timeout()
        if action.method == "fill":
            target.fill(action.value or "", timeout=timeout)
            expect(target).to_have_value(action.value or "", timeout=timeout)
        elif action.method == "select":
            selected = [option for option in control.options if option.label == action.value and not option.disabled]
            if len(selected) != 1:
                raise stop(
                    "choice_ambiguous", "The answer does not identify one enabled option.", "Review the choices."
                )
            target.select_option(value=selected[0].value, timeout=timeout)
            expect(target).to_have_value(selected[0].value, timeout=timeout)
        elif action.method == "check":
            if control.kind in ("radio", "checkbox"):
                target.set_checked(action.value == "true", timeout=timeout)
                expect(target).to_be_checked(checked=action.value == "true", timeout=timeout)
            else:
                target.click(timeout=timeout)
                expect(target).to_have_attribute("aria-checked", action.value or "true", timeout=timeout)
        else:
            target.click(timeout=timeout)
