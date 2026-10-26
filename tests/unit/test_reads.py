"""Read retries are bounded, honor short throttles and never expose transport errors."""

import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import APIResponse
from playwright.sync_api import Error as BrowserError

from prior_auth_agent.agent.proposals import ReadRetrySettings
from prior_auth_agent.browser.reads import ReadRetrier
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage.audit import ActionLog


def response(status: int, retry_after: str = "") -> Mock:
    result = Mock(spec=APIResponse)
    result.status = status
    result.headers = {"retry-after": retry_after} if retry_after else {}
    return result


def test_read_retries_a_throttle_and_transport_failure_with_controlled_logs(tmp_path: Path) -> None:
    log = ActionLog(tmp_path / "actions.jsonl")
    first, last = response(429, "0"), response(200)
    operation = Mock(side_effect=[first, BrowserError("SENSITIVE_TRANSPORT_DETAIL"), last])
    try:
        result = ReadRetrier(ReadRetrySettings(delay_ms=0), time.monotonic() + 10, 1000, log).run(operation)
    finally:
        log.close()
    assert result is last and operation.call_count == 3
    first.dispose.assert_called_once()
    events = (tmp_path / "actions.jsonl").read_text()
    assert events.count('"event": "portal_read_retry"') == 2
    assert "SENSITIVE_TRANSPORT_DETAIL" not in events


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_read_status_returns_once(status: int) -> None:
    rejected = response(status)
    operation = Mock(return_value=rejected)
    assert ReadRetrier(ReadRetrySettings(delay_ms=0), time.monotonic() + 10, 1000).run(operation) is rejected
    assert operation.call_count == 1


def test_read_retry_exhaustion_is_bounded() -> None:
    unavailable = response(503)
    operation = Mock(return_value=unavailable)
    assert ReadRetrier(ReadRetrySettings(delay_ms=0), time.monotonic() + 10, 1000).run(operation) is unavailable
    assert operation.call_count == 3


@pytest.mark.parametrize("delay", ["600", "invalid", "-1", "nan"])
def test_unusable_retry_after_escalates_without_another_request(delay: str) -> None:
    operation = Mock(return_value=response(429, delay))
    with pytest.raises(InputProblem) as raised:
        ReadRetrier(ReadRetrySettings(delay_ms=0), time.monotonic() + 10, 1000).run(operation)
    assert raised.value.issue.code == "retry_after_exceeds_budget"
    assert operation.call_count == 1


def test_http_date_retry_after_is_supported() -> None:
    first, last = response(429, "Sun, 06 Nov 1994 08:49:37 GMT"), response(200)
    operation = Mock(side_effect=[first, last])
    assert ReadRetrier(ReadRetrySettings(delay_ms=0), time.monotonic() + 10, 1000).run(operation) is last
    assert operation.call_count == 2
