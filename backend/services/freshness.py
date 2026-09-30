"""Posting freshness: fresh / stale / unknown. A missing post date is unknown, never stale."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement

from database.enums import Freshness
from database.models import Job


def compute_freshness(posted_at: datetime | None, now: datetime, max_age_hours: int) -> Freshness:
    if posted_at is None:
        return Freshness.UNKNOWN
    return Freshness.FRESH if posted_at >= now - timedelta(hours=max_age_hours) else Freshness.STALE


def freshness_filter(value: Freshness, now: datetime, max_age_hours: int) -> ColumnElement[Any]:
    """SQL predicate equivalent to compute_freshness(...) == value."""
    cutoff = now - timedelta(hours=max_age_hours)
    if value is Freshness.UNKNOWN:
        return Job.posted_at.is_(None)
    if value is Freshness.FRESH:
        return Job.posted_at >= cutoff
    return Job.posted_at < cutoff
