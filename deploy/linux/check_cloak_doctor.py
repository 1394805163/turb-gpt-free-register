#!/usr/bin/env python3
"""Validate the launch-critical fields from ``cloakbrowser doctor --json``."""

from __future__ import annotations

import json
import sys
from typing import Any


def require_true(payload: dict[str, Any], section: str, field: str) -> None:
    value = payload.get(section)
    if not isinstance(value, dict) or value.get(field) is not True:
        raise ValueError(f"{section}.{field} must be true")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("doctor JSON root must be an object")
        require_true(payload, "binary", "installed")
        require_true(payload, "launch", "tested")
        require_true(payload, "launch", "ok")
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Cloak doctor validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
