"""
Canonical PreMid/PostMid period strings for verification and API grouping.
"""
from typing import Optional


def normalize_period(p: Optional[str]) -> str:
    """
    Map CSV/API period labels to 'PRE' or 'POST' so faculty/section checks
    treat PreMid/PREMID/PostMid/POSTMID as the same half-semester bucket.

    Empty or unknown values default to PRE (matches historical CSV default).
    """
    v = (p or "").strip().upper().replace(" ", "").replace("-", "")
    if v in ("PRE", "PREMID", "PREMIDTERM"):
        return "PRE"
    if v in ("POST", "POSTMID", "POSTMIDTERM"):
        return "POST"
    if not v:
        return "PRE"
    return v
