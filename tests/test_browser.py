"""Real Chromium against isolated synthetic HTTP fixtures; no external portal access."""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from prior_auth_agent.audit import ActionLog
from prior_auth_agent.browser import PortalBrowser
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.inspection import inspection_loop
from prior_auth_agent.planner import ReviewedPlanner
from prior_auth_agent.proposals import candidates, load_browser_settings


@pytest.fixture
def portal() -> Iterator[tuple[str, list[str]]]:
    writes: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def page(self, body: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self) -> None:
            if self.path == "/dashboard":
                self.page('<h1>Dashboard</h1><a href="/create">New request</a><button>Send</button>')
            elif self.path == "/password":
                self.page(
                    '<form action="/signin/password" method="post">'
                    '<label for="p">Password</label><input id="p" type="password">'
                    "<button>Sign in</button></form>"
                )
            else:
                self.page(
                    '<form action="/signin" method="post">'
                    '<label for="u">Username</label><input id="u" name="username" type="text">'
                    "<button>Continue</button></form>"
                )

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            writes.append(self.path)
            self.send_response(303)
            self.send_header("Location", "/password" if self.path == "/signin" else "/dashboard")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", writes
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def credentials() -> Credentials:
    return Credentials(username="SYNTHETIC_USER", password="SYNTHETIC_PASSWORD")


def test_real_browser_signs_in_and_never_creates_or_sends(portal: tuple[str, list[str]], tmp_path: Path) -> None:
    url, writes = portal
    settings = load_browser_settings()
    log = ActionLog(tmp_path / "inspection.jsonl")
    try:
        with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
            result = inspection_loop(
                browser, ReviewedPlanner(), settings, credentials(), log, False, time.monotonic() + 30
            )
        assert result.code == "request_writes_disabled"
        assert writes == ["/signin", "/signin/password"]
    finally:
        log.close()
    contents = (tmp_path / "inspection.jsonl").read_text()
    assert "SYNTHETIC_" not in contents
    assert "inspection_complete" in contents


def test_role_label_and_subject_survive_different_layouts(portal: tuple[str, list[str]]) -> None:
    url, _ = portal
    settings = load_browser_settings()
    with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
        for html in (
            '<fieldset><legend>Patient</legend><label>First name<input type="text"></label></fieldset>',
            '<section aria-label="Patient information"><div>'
            '<input aria-label="Given name" type="text"></div></section>',
        ):
            browser.page.set_content(html)
            matched = candidates(browser.observe(), settings)
            assert len(matched) == 1
            assert matched[0].binding_id == "patient.first_name"


def test_network_guard_blocks_unsolicited_writes(portal: tuple[str, list[str]]) -> None:
    url, writes = portal
    with PortalBrowser(url, [], load_browser_settings(), time.monotonic() + 30) as browser:
        browser.page.evaluate("fetch('/create', {method:'POST'}).catch(()=>null)")
        assert writes == []
        with pytest.raises(InputProblem) as raised:
            browser.observe()
        assert raised.value.issue.code == "network_blocked"


def test_stale_page_and_fact_write_are_blocked(portal: tuple[str, list[str]]) -> None:
    url, writes = portal
    settings = load_browser_settings()
    with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
        old = browser.observe()
        browser.page.get_by_label("Username").fill("changed")
        with pytest.raises(InputProblem) as raised:
            browser.execute(settings.bindings[0], old, "c0", credentials())
        assert raised.value.issue.code == "page_changed"
        current = browser.observe()
        fact = next(binding for binding in settings.bindings if binding.purpose == "fact")
        with pytest.raises(InputProblem) as raised:
            browser.execute(fact, current, "c0", credentials())
        assert raised.value.issue.code == "request_writes_disabled"
        assert writes == []


def test_unreviewed_form_destination_cannot_receive_credentials(portal: tuple[str, list[str]]) -> None:
    url, writes = portal
    settings = load_browser_settings()
    with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
        browser.page.locator("form").evaluate("(f)=>f.action='/create'")
        observation = browser.observe()
        selected = candidates(observation, settings)[0]
        with pytest.raises(InputProblem) as raised:
            browser.execute(settings.bindings[0], observation, selected.control_ref, credentials())
        assert raised.value.issue.code == "auth_form_unreviewed"
        assert browser.page.get_by_label("Username").input_value() == ""
        assert writes == []


def test_five_real_browser_repeats_have_identical_logs(portal: tuple[str, list[str]], tmp_path: Path) -> None:
    url, writes = portal
    settings = load_browser_settings()
    results = []
    for index in range(5):
        path = tmp_path / f"repeat-{index}.jsonl"
        log = ActionLog(path)
        try:
            with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
                issue = inspection_loop(
                    browser, ReviewedPlanner(), settings, credentials(), log, False, time.monotonic() + 30
                )
        finally:
            log.close()
        results.append((issue, path.read_bytes()))
    assert all(result == results[0] for result in results)
    assert writes == ["/signin", "/signin/password"] * 5
    assert all(
        event["sequence"] == i
        for i, event in enumerate([json.loads(line) for line in results[0][1].splitlines()], start=1)
    )
