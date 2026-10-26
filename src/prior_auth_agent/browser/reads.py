"""Bounded retries for reads only; writes are never replayed here."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from playwright.sync_api import APIResponse
from playwright.sync_api import Error as BrowserError

from prior_auth_agent.agent.proposals import ReadRetrySettings, stop
from prior_auth_agent.storage.audit import ActionLog


class ReadRetrier:
    def __init__(
        self, settings: ReadRetrySettings, deadline: float, wait_ms: int, log: ActionLog | None = None
    ) -> None:
        self.settings = settings
        self.deadline = deadline
        self.wait_ms = wait_ms
        self.log = log

    def run(self, operation: Callable[[float], APIResponse]) -> APIResponse:
        for attempt in range(self.settings.attempts):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise stop(
                    "read_deadline",
                    "The portal read budget was exhausted.",
                    "Review portal availability.",
                    "portal.access",
                )
            response = None
            try:
                response = operation(min(self.wait_ms, remaining * 1000))
            except BrowserError:
                if attempt == self.settings.attempts - 1:
                    raise stop(
                        "portal_read_unavailable",
                        "A portal read failed after the bounded retry limit.",
                        "Review portal availability before continuing; do not repeat pending writes.",
                        "portal.access",
                    ) from None
            else:
                if response.status not in self.settings.statuses or attempt == self.settings.attempts - 1:
                    return response
            headers = response.headers if response is not None else {}
            status = str(response.status) if response is not None else "transport_error"
            if response is not None:
                response.dispose()
            self.pause(attempt, headers, status)
        raise AssertionError("The bounded read loop must return or escalate.")

    def pause(self, attempt: int, headers: dict[str, str], status: str) -> None:
        delay_ms = self.settings.delay_ms * (attempt + 1)
        retry_after = headers.get("retry-after", "")
        if retry_after:
            try:
                requested = float(retry_after) * 1000
            except ValueError:
                try:
                    target = parsedate_to_datetime(retry_after)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=UTC)
                    requested = max(0, (target - datetime.now(UTC)).total_seconds() * 1000)
                except (ValueError, TypeError, OverflowError):
                    requested = self.settings.max_retry_after_ms + 1
            if not 0 <= requested <= self.settings.max_retry_after_ms:
                raise stop(
                    "retry_after_exceeds_budget",
                    "The portal's retry delay exceeds the reviewed run budget.",
                    "Wait for the portal to recover, then review the case state before continuing.",
                    "portal.access",
                )
            delay_ms = max(delay_ms, requested)
        if delay_ms >= (self.deadline - time.monotonic()) * 1000:
            raise stop(
                "read_deadline",
                "A read retry would exceed the run budget.",
                "Review portal availability.",
                "portal.access",
            )
        if self.log is not None:
            self.log.event("portal_read_retry", attempt=attempt + 1, status=status)
        time.sleep(delay_ms / 1000)
