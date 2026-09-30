"""Phase 5.5: when the daily run fires (in-process scheduler), and how it is switched off."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import backend.main as main
from backend.core.config import Settings
from backend.services.daily import DailyScheduler, next_slot, parse_run_time, slot_for
from backend.services.runs import RunConflictError
from database.enums import AgentRunStatus, RunType
from database.models import AgentRun

IST = ZoneInfo("Asia/Kolkata")
AT = time(7, 0)


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now.astimezone(UTC)


def scheduler(factory: sessionmaker[Session], clock: Clock, trigger: Any) -> DailyScheduler:
    return DailyScheduler(at=AT, tz=IST, factory=factory, trigger=trigger, clock=clock)


def record_daily_run(factory: sessionmaker[Session], started: datetime) -> None:
    with factory() as db:
        db.add(
            AgentRun(
                task="Daily discovery & analysis (scheduled)",
                run_type=RunType.DAILY,
                status=AgentRunStatus.COMPLETED,
                started_at=started.astimezone(UTC),
                ended_at=started.astimezone(UTC),
            )
        )
        db.commit()


# --- time arithmetic -------------------------------------------------------------------------


def test_default_time_parses_and_rejects_garbage() -> None:
    assert parse_run_time("07:00") == time(7, 0)
    assert parse_run_time(" 23:45 ") == time(23, 45)
    assert parse_run_time("6") == time(6, 0)
    for bad in ("25:00", "07:61", "seven", ""):
        with pytest.raises(ValueError):
            parse_run_time(bad)


def test_next_slot() -> None:
    assert next_slot(ist(29, 6, 59), AT) == ist(29, 7)
    assert next_slot(ist(29, 7), AT) == ist(30, 7)  # at the slot: the next one is tomorrow
    assert next_slot(ist(29, 22), AT) == ist(30, 7)
    assert slot_for(ist(29, 22), AT) == ist(29, 7)


# --- scheduling decisions ----------------------------------------------------------------------


def test_waits_until_the_slot(session_factory: sessionmaker[Session]) -> None:
    calls: list[int] = []
    s = scheduler(session_factory, Clock(ist(29, 6, 30)), lambda: calls.append(1))
    outcome, wait = s.tick()
    assert outcome == "waiting" and wait == timedelta(minutes=30) and calls == []


def test_runs_once_the_slot_has_passed_then_waits_for_tomorrow(
    session_factory: sessionmaker[Session],
) -> None:
    clock = Clock(ist(29, 7, 0))
    calls: list[int] = []

    def trigger() -> None:
        calls.append(1)
        record_daily_run(session_factory, clock.now)

    s = scheduler(session_factory, clock, trigger)
    assert s.tick()[0] == "ran" and calls == [1]
    clock.now = ist(29, 7, 5)
    outcome, wait = s.tick()
    assert outcome == "done_today" and calls == [1]  # never twice for the same slot
    assert wait <= DailyScheduler.MAX_SLEEP
    clock.now = ist(30, 7, 0)
    assert s.tick()[0] == "ran" and calls == [1, 1]  # next day


def test_catches_up_when_the_machine_was_off_at_the_slot(
    session_factory: sessionmaker[Session],
) -> None:
    record_daily_run(session_factory, ist(28, 7, 1))  # yesterday's run
    calls: list[int] = []
    s = scheduler(session_factory, Clock(ist(29, 18, 0)), lambda: calls.append(1))
    assert s.tick()[0] == "ran" and calls == [1]


def test_a_manual_run_after_the_slot_counts_for_the_day(
    session_factory: sessionmaker[Session],
) -> None:
    record_daily_run(session_factory, ist(29, 8, 0))
    calls: list[int] = []
    s = scheduler(session_factory, Clock(ist(29, 9, 0)), lambda: calls.append(1))
    assert s.tick()[0] == "done_today" and calls == []


def test_a_manual_run_before_the_slot_does_not_skip_the_day(
    session_factory: sessionmaker[Session],
) -> None:
    record_daily_run(session_factory, ist(29, 6, 0))
    calls: list[int] = []
    s = scheduler(session_factory, Clock(ist(29, 7, 1)), lambda: calls.append(1))
    assert s.tick()[0] == "ran"


def test_busy_retries_later(session_factory: sessionmaker[Session]) -> None:
    def busy() -> None:
        raise RunConflictError("Run x (discovery) is still in progress")

    s = scheduler(session_factory, Clock(ist(29, 7, 30)), busy)
    assert s.tick() == ("busy", DailyScheduler.RETRY)


def test_a_failing_trigger_does_not_kill_the_scheduler(
    session_factory: sessionmaker[Session],
) -> None:
    def broken() -> None:
        raise RuntimeError("config file missing")

    s = scheduler(session_factory, Clock(ist(29, 7, 30)), broken)
    assert s.tick()[0] == "error"


def test_thread_runs_the_trigger_and_stops(session_factory: sessionmaker[Session]) -> None:
    fired = threading.Event()
    clock = Clock(ist(29, 7, 30))

    def trigger() -> None:
        record_daily_run(session_factory, clock.now)
        fired.set()

    s = scheduler(session_factory, clock, trigger)
    s.start()
    try:
        assert fired.wait(5)
    finally:
        s.stop()
    assert s._thread is None


# --- enabling / disabling ----------------------------------------------------------------------


class FakeScheduler:
    def __init__(self) -> None:
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeScheduler]:
    fake = FakeScheduler()
    monkeypatch.setattr(main, "daily_scheduler", lambda settings: fake)
    yield fake


def test_api_process_starts_and_stops_the_schedule(fake: FakeScheduler) -> None:
    with TestClient(main.create_app(Settings(daily_run_enabled=True))):
        assert fake.started and not fake.stopped
    assert fake.stopped


def test_disabled_schedule_never_starts(fake: FakeScheduler) -> None:
    with TestClient(main.create_app(Settings(daily_run_enabled=False))):
        pass
    assert not fake.started


def test_real_scheduler_uses_settings() -> None:
    s = main.daily_scheduler(Settings(daily_run_time="06:15", timezone="Asia/Kolkata"))
    assert s.at == time(6, 15) and s.tz.key == "Asia/Kolkata"
    assert s._thread is None  # built, not started


def test_defaults() -> None:
    settings = Settings()
    assert settings.daily_run_time == "07:00" and settings.daily_run_analysis_limit == 50
    assert settings.development_mode is True and settings.external_live_enabled is False
