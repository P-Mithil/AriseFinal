"""Helpers for Phase 4 / Phase 7 period prompts in CLI and automated runs."""

from __future__ import annotations

import os
import sys


def skip_interactive_prompts() -> bool:
    """
    True when stdin is not a TTY, CI is set, or ARISE_NONINTERACTIVE is truthy.
    In those cases callers should not block on input().
    """
    if os.environ.get("ARISE_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("CI", "").strip():
        return True
    try:
        if not sys.stdin.isatty():
            return True
    except Exception:
        return True
    return False


def default_period_is_pre_mid(env_key: str) -> bool:
    """
    Map env value to PreMid (True) vs PostMid (False).
    Reads env_key first, then ARISE_DEFAULT_PERIOD, else PreMid.
    """
    raw = (os.environ.get(env_key) or os.environ.get("ARISE_DEFAULT_PERIOD") or "pre").strip().lower()
    if raw in ("2", "post", "postmid", "post_mid", "post-mid"):
        return False
    return True
