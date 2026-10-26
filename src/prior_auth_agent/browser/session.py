"""Browser observations and narrowly authorized synthetic authentication."""

import time
from importlib.resources import files
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Dialog, Locator, Page, Request, Route, expect, sync_playwright

from prior_auth_agent.agent.proposals import Binding, BrowserSettings, Control, Observation, digest, normalize, stop
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.validation import validate_url

OBSERVE_SCRIPT = files("prior_auth_agent.browser").joinpath("observe.js").read_text(encoding="utf-8")


def origin(url: str) -> str:
    validate_url(url)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname or ""
    return f"{parsed.scheme}://{host}:{port}"


class NetworkGuard:
    """Only trusted GET/HEAD requests and a single authorized auth POST may pass."""

    def __init__(self, origins: set[str], authentication_paths: list[str]) -> None:
        self.origins = origins
        self.authentication_paths = authentication_paths
        self.grant: tuple[str, str] | None = None
        self.blocked = False

    def route(self, route: Route, request: Request) -> None:
        parsed = urlsplit(request.url)
        trusted = parsed.scheme in ("http", "https") and origin(request.url) in self.origins
        read = request.method in ("GET", "HEAD")
        granted = (
            trusted
            and request.method == "POST"
            and request.is_navigation_request()
            and parsed.path in self.authentication_paths
            and not parsed.query
            and (origin(request.url), parsed.path) == self.grant
        )
        if granted:
            self.grant = None
        if trusted and (read or granted):
            route.continue_()
        else:
            if not read or request.is_navigation_request():
                self.blocked = True
            route.abort("blockedbyclient")


class PortalBrowser:
    def __init__(self, start_url: str, auth_origins: list[str], settings: BrowserSettings, deadline: float) -> None:
        self.start_url = start_url
        self.settings = settings
        self.deadline = deadline
        self.guard = NetworkGuard(
            {origin(start_url), *(origin(url) for url in auth_origins)}, settings.authentication_paths
        )
        self.unexpected_surface = False

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise stop(
                "run_deadline",
                "The inspection time limit was reached.",
                "Inspect the portal manually before continuing.",
            )
        return min(self.settings.wait_ms, remaining * 1000)

    def __enter__(self) -> "PortalBrowser":
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(timeout=self.timeout())
            self.context: BrowserContext = self.browser.new_context(service_workers="block", accept_downloads=False)
            self.context.route("**/*", self.guard.route)
            self.context.route_web_socket("**/*", lambda socket: socket.close())
            self.page: Page = self.context.new_page()
            self.page.on("dialog", self._dialog)
            self.context.on("page", self._popup)
            self.page.goto(self.start_url, wait_until="domcontentloaded", timeout=self.timeout())
            self.page.wait_for_function(
                """headings => [...document.querySelectorAll('h1,h2,h3,[role=heading]')].some(
                    e => headings.includes(e.textContent.trim()) && e.getClientRects().length
                ) || [...document.querySelectorAll('form input')].some(
                    e => e.type !== 'hidden' && e.getClientRects().length
                )""",
                arg=self.settings.authenticated_headings,
                timeout=self.timeout(),
            )
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _dialog(self, dialog: Dialog) -> None:
        # Unknown dialogs cannot authorize an attestation or other action.
        self.unexpected_surface = True
        dialog.dismiss()

    def _popup(self, page: Page) -> None:
        self.unexpected_surface = True
        page.close()

    def __exit__(self, *args: object) -> None:
        try:
            if hasattr(self, "browser"):
                self.browser.close()
        finally:
            self.playwright.stop()

    def observe(self) -> Observation:
        self.timeout()
        if self.guard.blocked:
            raise stop(
                "network_blocked",
                "An unapproved origin or write request was blocked.",
                "Review the portal origin and authentication configuration.",
                "portal.access",
            )
        if self.unexpected_surface:
            raise stop(
                "unexpected_surface",
                "An unreviewed popup or dialog interrupted the portal.",
                "Resolve the popup or dialog manually.",
                "portal.access",
            )
        if origin(self.page.url) not in self.guard.origins:
            raise stop(
                "origin_untrusted",
                "The portal navigated outside its reviewed origins.",
                "Review the sign-in origin before continuing.",
                "portal.access",
            )
        raw = self.page.evaluate(OBSERVE_SCRIPT)
        controls = [Control.model_validate(control) for control in raw]
        authenticated = any(
            self.page.get_by_role("heading", name=heading, exact=True).count() == 1
            and self.page.get_by_role("heading", name=heading, exact=True).is_visible()
            for heading in self.settings.authenticated_headings
        ) and not any(
            control.input_type == "password"
            or normalize(control.label)
            in {
                normalize(label)
                for binding in self.settings.bindings
                if binding.purpose in ("username", "password")
                for label in binding.labels
            }
            for control in controls
        )
        fingerprint = digest(
            {
                "location": self.page.url,
                "controls": [control.model_dump() for control in controls],
                "authenticated": authenticated,
            }
        )
        return Observation(fingerprint=fingerprint, controls=controls, authenticated=authenticated)

    def _locate(self, binding: Binding, observation: Observation, ref: str) -> Locator:
        control = next((control for control in observation.controls if control.ref == ref), None)
        if control is None:
            raise stop(
                "target_missing", "The selected control is no longer available.", "Review the current portal page."
            )
        if binding.role == "button":
            target = self.page.get_by_role("button", name=control.label, exact=True)
        else:
            target = self.page.get_by_label(control.label, exact=True)
        if target.count() != 1 or not target.is_visible() or not target.is_enabled():
            raise stop(
                "target_ambiguous",
                "The selected control is not a single visible, enabled field.",
                "Review the field binding before continuing.",
            )
        return target

    def _auth_form(self, target: Locator, binding: Binding) -> tuple[str, str]:
        form = target.locator("xpath=ancestor::form[1]")
        if form.count() != 1:
            raise stop(
                "auth_form_unreviewed", "The sign-in control is not in a reviewed form.", "Review the sign-in form."
            )
        destination = form.evaluate("(form) => ({action: form.action, method: form.method.toUpperCase()})")
        parsed = urlsplit(destination["action"])
        if (
            destination["method"] != "POST"
            or parsed.path not in self.settings.authentication_paths
            or (origin(destination["action"]) not in self.guard.origins)
            or parsed.query
            or parsed.fragment
        ):
            raise stop(
                "auth_form_unreviewed",
                "The sign-in destination is not reviewed.",
                "Review the authentication destination and allowed origins.",
                "portal.access",
            )
        # Credential labels alone are insufficient: check the credential type and paired submit button in the same form.
        visible_fields = form.locator('input:not([type="hidden"]):visible')
        if visible_fields.count() != 1:
            raise stop(
                "auth_form_unreviewed",
                "This sign-in form is not a reviewed single-credential step.",
                "Review the authentication form before entering credentials.",
            )
        expected_purpose = "username" if binding.purpose in ("username", "continue") else "password"
        field_bindings = [item for item in self.settings.bindings if item.purpose == expected_purpose]
        fields = [
            form.get_by_label(label, exact=True)
            for item in field_bindings
            for label in item.labels
            if form.get_by_label(label, exact=True).count() == 1
        ]
        if len(fields) != 1 or fields[0].get_attribute("type") not in {
            kind for item in field_bindings for kind in item.input_types
        }:
            raise stop(
                "auth_form_unreviewed", "The sign-in field type or meaning is ambiguous.", "Review the sign-in form."
            )
        submit_purpose = "continue" if expected_purpose == "username" else "sign_in"
        buttons = [
            form.get_by_role("button", name=label, exact=True)
            for item in self.settings.bindings
            if item.purpose == submit_purpose
            for label in item.labels
            if form.get_by_role("button", name=label, exact=True).count() == 1
        ]
        if len(buttons) != 1:
            raise stop("auth_form_unreviewed", "The sign-in submit button is ambiguous.", "Review the sign-in form.")
        return origin(destination["action"]), parsed.path

    def execute(self, binding: Binding, observation: Observation, ref: str, credentials: Credentials) -> None:
        # Re-observe immediately before resolving a target. No nth()/IDs/coordinates from the model.
        if self.observe().fingerprint != observation.fingerprint:
            raise stop("page_changed", "The page changed before execution.", "Review the current portal page.")
        if binding.purpose == "fact":
            raise stop(
                "request_writes_disabled",
                "Request field writes are disabled in this milestone.",
                "Continue only after duplicate protection and submission recovery are implemented.",
            )
        target = self._locate(binding, observation, ref)
        destination = self._auth_form(target, binding)
        if binding.purpose in ("username", "password"):
            secret = getattr(credentials, binding.purpose).get_secret_value()
            target.fill(secret, timeout=self.timeout())
            expect(target).to_have_value(secret, timeout=self.timeout())
        else:
            purpose = "username" if binding.purpose == "continue" else "password"
            field = target.locator("xpath=ancestor::form[1]").locator('input:not([type="hidden"]):visible')
            expect(field).to_have_value(getattr(credentials, purpose).get_secret_value(), timeout=self.timeout())
            self.guard.grant = destination
            try:
                target.click(timeout=self.timeout())
                self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout())
                if self.guard.grant is not None:
                    raise stop(
                        "authentication_unverified",
                        "The reviewed authentication request was not observed.",
                        "Review the sign-in step before continuing.",
                        "portal.access",
                    )
            finally:
                self.guard.grant = None
