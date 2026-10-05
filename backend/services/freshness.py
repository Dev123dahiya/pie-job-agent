"""Posting freshness: fresh / stale / unknown. A missing post date is unknown, never stale.

The window is the user's preference (maximum_application_age_hours), except for sources whose
postings stay open much longer, listed in SOURCE_MAX_AGE_HOURS.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, or_

from database.enums import Freshness
from database.models import Job

# Instahyre lists jobs for months and has no "newest first" sort; with the 72-hour window it
# would find almost nothing. The user chose 14 days for it (2026-10-05).
SOURCE_MAX_AGE_HOURS: dict[str, int] = {"instahyre": 14 * 24}


def window_hours(source: str | None, max_age_hours: int) -> int:
    return SOURCE_MAX_AGE_HOURS.get((source or "").lower(), max_age_hours)


def compute_freshness(
    posted_at: datetime | None, now: datetime, max_age_hours: int, source: str | None = None
) -> Freshness:
    if posted_at is None:
        return Freshness.UNKNOWN
    cutoff = now - timedelta(hours=window_hours(source, max_age_hours))
    return Freshness.FRESH if posted_at >= cutoff else Freshness.STALE


def freshness_filter(value: Freshness, now: datetime, max_age_hours: int) -> ColumnElement[Any]:
    """SQL predicate equivalent to compute_freshness(..., job.source) == value."""
    if value is Freshness.UNKNOWN:
        return Job.posted_at.is_(None)

    def side(cutoff: datetime) -> ColumnElement[Any]:
        return Job.posted_at >= cutoff if value is Freshness.FRESH else Job.posted_at < cutoff

    default = and_(
        Job.source.not_in(list(SOURCE_MAX_AGE_HOURS)),
        side(now - timedelta(hours=max_age_hours)),
    )
    special = [
        and_(Job.source == source, side(now - timedelta(hours=hours)))
        for source, hours in SOURCE_MAX_AGE_HOURS.items()
    ]
    return or_(default, *special)
