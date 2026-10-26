"""Own fresh practice servers; observe records through public read endpoints only."""

import json
import socket
import subprocess
import time
from pathlib import Path
from types import TracebackType
from urllib.error import URLError
from urllib.request import urlopen

from pydantic import JsonValue

from prior_auth_agent.agent.inspection import inspection_loop
from prior_auth_agent.agent.planner import ReviewedPlanner
from prior_auth_agent.agent.proposals import load_browser_settings
from prior_auth_agent.browser.session import PortalBrowser
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.storage.audit import ActionLog

PORTAL_URL = "http://127.0.0.1:9463/dashboard"
BASE_URL = "http://127.0.0.1:9463"
AUTH_ORIGIN = "http://127.0.0.1:9464"


def occupied(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        return connection.connect_ex(("127.0.0.1", port)) == 0


class FreshPortal:
    def __init__(self, repository: Path, python: Path, directory: Path, seed: int) -> None:
        self.repository, self.python, self.directory, self.seed = repository, python, directory, seed
        self.process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "FreshPortal":
        if occupied(9463) or occupied(9464):
            raise RuntimeError("Practice portal ports are occupied; the evaluator will not stop another server.")
        log = self.directory / "portal.log"
        log.touch(mode=0o600, exist_ok=False)
        with log.open("wb") as stream:
            self.process = subprocess.Popen(
                [str(self.python), "-m", "portals", "--variance", "off", "--seed", str(self.seed)],
                cwd=self.repository,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("The fresh portal process exited during startup; inspect its private log.")
                try:
                    with urlopen(PORTAL_URL, timeout=0.5) as response:
                        if response.status == 200 and occupied(9464):
                            return self
                except (URLError, TimeoutError, OSError):
                    pass
                time.sleep(0.1)
            raise RuntimeError("The fresh portal did not become ready within 30 seconds.")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class Observer:
    """A separate session; does not trust the agent's receipt, case journal or duplicate decisions."""

    def __init__(self, credentials: Credentials, directory: Path) -> None:
        self.credentials, self.directory = credentials, directory

    def snapshot(
        self, known: set[str] | None = None, detail_ids: set[str] | None = None
    ) -> dict[str, dict[str, JsonValue]]:
        settings = load_browser_settings()
        log = ActionLog(
            self.directory / ("before-observer.actions.jsonl" if known is None else "after-observer.actions.jsonl")
        )
        try:
            with PortalBrowser(PORTAL_URL, [AUTH_ORIGIN], settings, time.monotonic() + 90, log) as browser:
                inspection_loop(browser, ReviewedPlanner(), settings, self.credentials, log, False, browser.deadline)
                rows: dict[str, dict[str, JsonValue]] = {}
                total: int | None = None
                for page in range(1, 11):
                    response = browser.reads.run(
                        lambda timeout, page=page: browser.page.request.get(
                            BASE_URL + "/api/requests", params={"page": str(page)}, timeout=timeout, max_redirects=0
                        )
                    )
                    try:
                        if not response.ok:
                            raise RuntimeError("The independent request list could not be read.")
                        payload = response.json()
                    finally:
                        response.dispose()
                    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                        raise RuntimeError("The public request-list contract is unsupported.")
                    count, pages = payload.get("total"), payload.get("pages")
                    if type(count) is not int or type(pages) is not int or payload.get("page") != page:
                        raise RuntimeError("Independent pagination could not be verified.")
                    if total is not None and total != count:
                        raise RuntimeError("The portal history changed during independent verification.")
                    total = count
                    for row in payload["rows"]:
                        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] in rows:
                            raise RuntimeError("The independent request list is incomplete or ambiguous.")
                        rows[row["key"]] = row
                    if page == pages or count == 0:
                        break
                if len(rows) != total:
                    raise RuntimeError("The independent request history exceeds its complete scan limit.")
                if known is None:
                    return rows
                if not known <= set(rows):
                    raise RuntimeError("Previously observed requests disappeared during independent verification.")
                targets = (set(rows) - known) | (detail_ids or set())
                details: dict[str, dict[str, JsonValue]] = {}
                for key in targets:
                    if key not in rows or not key.replace("-", "").isalnum():
                        raise RuntimeError("An independently observed request ID is unsupported.")
                    response = browser.reads.run(
                        lambda timeout, key=key: browser.page.request.get(
                            BASE_URL + "/api/requests/" + key, timeout=timeout, max_redirects=0
                        )
                    )
                    try:
                        payload = response.json() if response.ok else None
                    finally:
                        response.dispose()
                    request = payload.get("request") if isinstance(payload, dict) else None
                    if not isinstance(request, dict) or request.get("key") != key:
                        raise RuntimeError("The independent request detail could not be verified.")
                    details[key] = request
                # Full responses stay private; the shareable report contains counts and hashes.
                path = self.directory / "portal-evidence.json"
                path.touch(mode=0o600, exist_ok=False)
                path.write_text(json.dumps({"keys": sorted(rows), "details": details}, indent=2) + "\n")
                return details
        finally:
            log.close()
