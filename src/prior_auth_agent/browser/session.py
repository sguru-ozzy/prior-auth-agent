"""Browser observations and narrowly authorized synthetic authentication."""

import time
from importlib.resources import files
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Dialog, Locator, Page, Request, Response, Route, expect, sync_playwright
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import TimeoutError as BrowserTimeout

from prior_auth_agent.agent.proposals import Binding, BrowserSettings, Control, Observation, digest, normalize, stop
from prior_auth_agent.browser.reads import ReadRetrier
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.storage.audit import ActionLog
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
        self.ui_grant: tuple[str, str | None, tuple[str, ...]] | None = None
        self.ui_writes = 0
        self.ui_last_request: Request | None = None
        self.writes: dict[tuple[str, str, str], int] = {}

    def permit_write(self, destination: str, method: str, path: str, count: int = 1) -> None:
        self.writes[(origin(destination), method, path)] = count

    def permit_ui_action(self, destination: str, path: str | None = None, method: str | None = None) -> None:
        """One same-origin mutation caused by an authorized observed UI action; never a blanket grant."""
        self.ui_grant = (origin(destination), path, (method,) if method else ("POST", "PUT", "PATCH"))

    def revoke_writes(self) -> None:
        self.writes.clear()
        self.ui_grant = None

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
        permission = (origin(request.url), request.method, parsed.path) if trusted else None
        workflow_write = permission is not None and self.writes.get(permission, 0) > 0 and not parsed.query
        ui_write = (
            trusted
            and self.ui_grant is not None
            and origin(request.url) == self.ui_grant[0]
            and (self.ui_grant[1] is None or parsed.path == self.ui_grant[1])
            and request.method in self.ui_grant[2]
            and request.resource_type in ("document", "xhr", "fetch")
        )
        if ui_write:
            self.ui_grant = None
            self.ui_writes += 1
            self.ui_last_request = request
        if workflow_write and permission is not None:
            self.writes[permission] -= 1
        if granted:
            self.grant = None
        if trusted and (read or granted or workflow_write or ui_write):
            route.continue_()
        else:
            if not read or request.is_navigation_request():
                self.blocked = True
            route.abort("blockedbyclient")


class PortalBrowser:
    def __init__(
        self,
        start_url: str,
        auth_origins: list[str],
        settings: BrowserSettings,
        deadline: float,
        log: ActionLog | None = None,
        semantic: bool = False,
    ) -> None:
        self.start_url = start_url
        self.semantic = semantic
        self.settings = settings
        self.deadline = deadline
        self.log = log
        self.overlay_dismissals = 0
        self.read_error_headers: dict[str, dict[str, str]] = {}
        self.reads = ReadRetrier(settings.read_retry, deadline, settings.wait_ms, log)
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

    def read_timeout(self) -> float:
        self.timeout()
        retry = self.settings.read_retry
        budget = self.settings.wait_ms * retry.attempts + retry.max_retry_after_ms * (retry.attempts - 1)
        return min(budget, max(1, (self.deadline - time.monotonic()) * 1000))

    def __enter__(self) -> "PortalBrowser":
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(timeout=self.timeout())
            self.context: BrowserContext = self.browser.new_context(service_workers="block", accept_downloads=False)
            self.context.route("**/*", self.guard.route)
            self.context.route_web_socket("**/*", lambda socket: socket.close())
            self.page: Page = self.context.new_page()
            self.page.on("dialog", self._dialog)
            self.page.on("response", self._read_response)
            self.context.on("page", self._popup)
            self.page.goto(self.start_url, wait_until="domcontentloaded", timeout=self.read_timeout())
            self.page.wait_for_function(
                """headings => [...document.querySelectorAll('h1,h2,h3,[role=heading]')].some(
                    e => (headings.length === 0 || headings.includes(e.textContent.trim())) && e.getClientRects().length
                ) || [...document.querySelectorAll(
                    headings.length ? 'form input' : 'input,textarea,button,[role=textbox]'
                )].some(
                    e => e.type !== 'hidden' && e.getClientRects().length
                )""",
                arg=[] if self.semantic else self.settings.authenticated_headings,
                timeout=self.timeout(),
            )
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read_response(self, response: Response) -> None:
        if response.request.method == "GET" and response.status in self.settings.read_retry.statuses:
            retry_after = response.headers.get("retry-after", "")
            self.read_error_headers[urlsplit(response.url).path] = {"retry-after": retry_after} if retry_after else {}

    def _dialog(self, dialog: Dialog) -> None:
        # Unknown dialogs cannot authorize an attestation or other action.
        self.unexpected_surface = True
        dialog.dismiss()

    def _popup(self, page: Page) -> None:
        self.unexpected_surface = True
        page.close()

    def __exit__(self, *args: object) -> None:
        try:
            if hasattr(self, "context"):
                try:
                    self.context.set_offline(True)
                    self.context.unroute_all(behavior="ignoreErrors")
                except BrowserError:
                    pass
            if hasattr(self, "browser"):
                self.browser.close()
        finally:
            self.playwright.stop()

    def dismiss_known_overlays(self) -> None:
        for rule in self.settings.known_overlays:
            while True:
                button = self.page.get_by_role("button", name=rule.dismiss, exact=True)
                if button.count() != 1 or not button.is_visible():
                    break
                containers = self.page.locator("div,section,aside").filter(has=button)
                matches = [
                    container
                    for container in containers.all()
                    if container.is_visible()
                    and not container.locator("input,select,textarea,form").count()
                    and normalize(
                        container.evaluate(
                            """e => {const nodes=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);const parts=[];
                               while(nodes.nextNode()){const n=nodes.currentNode;
                               if(n.parentElement && n.parentElement.getClientRects().length)parts.push(n.textContent);}
                               return parts.join(' ');}"""
                        )
                    )
                    == normalize(rule.text)
                ]
                if not matches:
                    break
                if self.overlay_dismissals >= self.settings.max_overlay_dismissals:
                    raise stop(
                        "overlay_limit",
                        "A known portal overlay keeps recurring.",
                        "Resolve the recurring overlay manually.",
                        "portal.access",
                    )
                button.click(timeout=self.timeout())
                self.overlay_dismissals += 1
                if self.log is not None:
                    self.log.event("known_overlay_dismissed", binding=rule.id, attempt=self.overlay_dismissals)
                try:
                    button.wait_for(state="hidden", timeout=min(300, self.timeout()))
                except BrowserTimeout:
                    continue
                break

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
        self.dismiss_known_overlays()
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
