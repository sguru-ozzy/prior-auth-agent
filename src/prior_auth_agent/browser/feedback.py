"""Bounded, visible portal validation feedback for private human handoffs."""

from playwright.sync_api import TimeoutError as BrowserTimeout

from prior_auth_agent.agent.proposals import normalize, stop
from prior_auth_agent.agent.questions import WorkflowSettings
from prior_auth_agent.browser.session import PortalBrowser
from prior_auth_agent.errors import InputProblem


def validation_rejection(
    browser: PortalBrowser, settings: WorkflowSettings, operation: str, status: int
) -> InputProblem:
    page = browser.page
    try:
        page.wait_for_function(
            """() => [...document.querySelectorAll('[role=alert],[aria-invalid=true]')]
                .some(e => e.getClientRects().length)""",
            timeout=min(1000, browser.timeout()),
        )
    except BrowserTimeout:
        pass
    mappings = {normalize(label): field for label, field in settings.validation_fields.items()}
    for rule in settings.question_rules:
        for label in rule.labels:
            existing = mappings.get(normalize(label))
            if existing is None or existing == rule.source:
                mappings[normalize(label)] = rule.source
            else:
                mappings[normalize(label)] = "portal.validation"
    fields: set[str] = set()
    messages: list[str] = []
    for control in page.locator('[aria-invalid="true"]:visible').all()[:20]:
        details = control.evaluate(
            r"""e => {
                const label = e.getAttribute('aria-label') || [...(e.labels || [])].map(x=>x.textContent).join(' ');
                const ids = e.getAttribute('aria-errormessage') || e.getAttribute('aria-describedby') || '';
                const message = ids.split(/\s+/).map(id=>document.getElementById(id))
                    .filter(x=>x && x.getClientRects().length).map(x=>x.textContent).join(' ');
                return {label, message};
            }"""
        )
        label = " ".join(str(details.get("label", "")).split())[:200]
        message = " ".join(str(details.get("message", "")).split())[:500]
        fields.add(mappings.get(normalize(label), "portal.validation"))
        messages.append((label + ": " if label else "") + (message or "The portal marks this field invalid."))
    for alert in page.get_by_role("alert").all()[:20]:
        if alert.is_visible():
            message = " ".join(alert.inner_text(timeout=browser.timeout()).split())[:500]
            if message and message not in messages:
                messages.append(message)
    field = next(iter(fields)) if len(fields) == 1 else "portal.validation"
    detail = "; ".join(dict.fromkeys(messages))[:2000]
    return stop(
        "portal_validation_rejected",
        f"The portal rejected {operation} (HTTP {status})."
        + (" " + detail if detail else " No field message is visible."),
        "Clinical staff must review the indicated field and portal message against the source record. "
        "Keep the journal and existing request; do not change facts to pass validation or repeat a pending write.",
        field,
    )
