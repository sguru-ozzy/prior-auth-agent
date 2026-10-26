"""Strict JSON loading and atomic, private artifact publication."""

import json
import math
import os
import stat
import tempfile
from pathlib import Path

from pydantic import JsonValue

from prior_auth_agent.errors import InputProblem, Issue


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number.")
    return number


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def load_object(path: Path, field: str, max_bytes: int) -> dict[str, JsonValue]:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise OSError("Expected a regular file.")
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError:
        raise InputProblem(
            Issue(field, "unreadable_input", "Input file could not be read.", "Provide a readable regular JSON file.")
        ) from None
    if len(content) > max_bytes:
        raise InputProblem(
            Issue(
                field,
                "input_too_large",
                "Input exceeds the configured size limit.",
                "Provide one record within the configured input size limit.",
            )
        )
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InputProblem(
            Issue(
                field,
                "invalid_json",
                "JSON must be valid UTF-8, with unique keys and finite numbers.",
                "Correct the source JSON; remove duplicate keys and non-finite numbers.",
            )
        ) from None
    if not isinstance(document, dict):
        raise InputProblem(
            Issue(
                field,
                "expected_object",
                "Expected one JSON object.",
                "Provide one patient record or credentials object, rather than an array.",
            )
        )
    return document


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
