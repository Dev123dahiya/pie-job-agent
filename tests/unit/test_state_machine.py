import pytest

from database.enums import JobPipelineState as S
from database.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    assert_transition,
    can_transition,
)

HAPPY_PATH = [
    S.DISCOVERED,
    S.ANALYZED,
    S.SHORTLISTED,
    S.READY_TO_APPLY,
    S.APPLICATION_IN_PROGRESS,
    S.READY_FOR_SUBMISSION,
    S.SUBMITTED,
]


def test_every_state_has_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_phase3_states_are_explicit() -> None:
    names = {s.value for s in S}
    assert {
        "DISCOVERED", "ANALYZED", "SHORTLISTED", "READY_TO_APPLY",
        "APPLICATION_IN_PROGRESS", "SUBMITTED", "FAILED", "BLOCKED_NEEDS_USER",
    } <= names  # fmt: skip


def test_happy_path_is_allowed() -> None:
    for current, nxt in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert_transition(current, nxt)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.ANALYZED, S.SUBMITTED),  # cannot skip straight to submitted
        (S.DISCOVERED, S.SUBMITTED),
        (S.SHORTLISTED, S.SUBMITTED),
        (S.READY_TO_APPLY, S.SUBMITTED),  # must go through a real in-progress application
        (S.SHORTLISTED, S.APPLICATION_IN_PROGRESS),
        (S.APPLICATION_IN_PROGRESS, S.SUBMITTED),  # must be prepared and validated first
        (S.ANALYZED, S.APPLICATION_IN_PROGRESS),
        (S.REJECTED_BY_AGENT, S.APPLICATION_IN_PROGRESS),
        (S.READY_TO_APPLY, S.READY_FOR_SUBMISSION),
        (S.SHORTLISTED, S.READY_FOR_SUBMISSION),
        (S.DUPLICATE, S.ANALYZED),
        (S.SUBMITTED, S.DISCOVERED),
    ],
)
def test_illegal_transitions_rejected(current: S, target: S) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError):
        assert_transition(current, target)


def test_blocked_states_can_resume() -> None:
    assert can_transition(S.APPLICATION_IN_PROGRESS, S.BLOCKED_NEEDS_USER)
    assert can_transition(S.BLOCKED_NEEDS_USER, S.APPLICATION_IN_PROGRESS)
    assert can_transition(S.BLOCKED_NEEDS_USER, S.READY_TO_APPLY)
    assert can_transition(S.FAILED, S.READY_TO_APPLY)


def test_reanalysis_and_preference_exclusion_transitions() -> None:
    assert can_transition(S.SHORTLISTED, S.ANALYZED)
    assert can_transition(S.REJECTED_BY_AGENT, S.ANALYZED)
    assert can_transition(S.DISCOVERED, S.REJECTED_BY_AGENT)
    assert not can_transition(S.SUBMITTED, S.ANALYZED)


def test_blocked_to_submitted_exists_only_for_user_resolution() -> None:
    # The transition exists so the USER can confirm an unconfirmed live submission; the
    # service guards it (see tests/integration/test_execution.py). No tool can make this move.
    assert can_transition(S.BLOCKED_NEEDS_USER, S.SUBMITTED)


def test_closing_a_session_returns_to_ready_to_apply() -> None:
    assert can_transition(S.APPLICATION_IN_PROGRESS, S.READY_TO_APPLY)
    assert can_transition(S.READY_FOR_SUBMISSION, S.READY_TO_APPLY)
