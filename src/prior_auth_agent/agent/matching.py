"""Deterministic accessible-name matching over reviewed equivalents only."""

import re


def name_pattern(primary: str, aliases: list[str] | None = None) -> re.Pattern[str]:
    names = [primary, *(aliases or [])]
    patterns = [r"\s+".join(re.escape(part) for part in name.split()) for name in names]
    return re.compile(r"^\s*(?:" + "|".join(sorted(set(patterns))) + r")\s*$", re.IGNORECASE)
