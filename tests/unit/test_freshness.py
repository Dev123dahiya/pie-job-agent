from sqlalchemy.orm import Session


def test_instahyre_uses_its_own_14_day_window() -> None:
    from datetime import UTC, datetime, timedelta

    from backend.services.freshness import compute_freshness
    from database.enums import Freshness

    now = datetime(2026, 10, 5, tzinfo=UTC)
    ten_days = now - timedelta(days=10)
    assert compute_freshness(ten_days, now, 72, "instahyre") is Freshness.FRESH
    assert compute_freshness(ten_days, now, 72, "naukri") is Freshness.STALE
    assert compute_freshness(now - timedelta(days=15), now, 72, "instahyre") is Freshness.STALE


def test_the_sql_filter_matches_the_per_source_window(db: Session) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from backend.services.freshness import compute_freshness, freshness_filter
    from database.base import utcnow
    from database.enums import Freshness, JobPipelineState
    from database.models import Job

    now = utcnow()
    jobs = [
        Job(
            source=s,
            source_job_id=f"{s}-{d}",
            title="Python Developer",
            url=f"https://x.invalid/{s}/{d}",
            posted_at=now - timedelta(days=d),
            pipeline_state=JobPipelineState.DISCOVERED,
        )  # fmt: skip
        for s in ("instahyre", "naukri")
        for d in (1, 10, 20)
    ]
    db.add_all(jobs)
    db.commit()
    for value in (Freshness.FRESH, Freshness.STALE):
        got = set(db.scalars(select(Job.source_job_id).where(freshness_filter(value, now, 72))))
        want = {
            j.source_job_id
            for j in jobs
            if compute_freshness(j.posted_at, now, 72, j.source) is value
        }
        assert got == want
    assert "instahyre-10" in set(
        db.scalars(select(Job.source_job_id).where(freshness_filter(Freshness.FRESH, now, 72)))
    )
