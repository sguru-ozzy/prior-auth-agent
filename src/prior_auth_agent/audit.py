"""Ordered JSON Lines events containing controlled metadata only."""

import json
import os
from datetime import date
from pathlib import Path
from typing import TextIO

from prior_auth_agent.errors import Issue


class ActionLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            self._stream: TextIO = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        self._sequence = 0

    def event(
        self,
        name: str,
        *,
        issue: Issue | None = None,
        status: str | None = None,
        reference_date: date | None = None,
        configuration_digest: str | None = None,
    ) -> None:
        self._sequence += 1
        event: dict[str, str | int] = {"sequence": self._sequence, "event": name}
        if reference_date is not None:
            event["reference_date"] = reference_date.isoformat()
        if configuration_digest is not None:
            event["configuration_digest"] = configuration_digest
        if issue is not None:
            event.update(field=issue.field, code=issue.code)
        if status is not None:
            event["status"] = status
        self._stream.write(json.dumps(event, sort_keys=True) + "\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        self._stream.close()
