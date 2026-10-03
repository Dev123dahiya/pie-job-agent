"""Allowed transitions for the job pipeline. Every state change is validated here.

    DISCOVERED -> ANALYZED -> SHORTLISTED -> READY_TO_APPLY -> APPLICATION_IN_PROGRESS
        -> READY_FOR_SUBMISSION -> SUBMITTED

plus REJECTED_BY_AGENT, BLOCKED_NEEDS_USER, FAILED, DUPLICATE, WITHDRAWN and MANUAL_APPLY
(the user applies themselves; only the user marks it applied). Skipping steps
(e.g. ANALYZED -> SUBMITTED) is never allowed. SUBMITTED can only follow READY_FOR_SUBMISSION,
and only a positively confirmed LIVE browser submission (backend/services/execution.py) makes
that move. Closing a browser session without submitting returns the job to READY_TO_APPLY.
"""

from database.enums import JobPipelineState as S

ALLOWED_TRANSITIONS: dict[S, set[S]] = {
    # REJECTED_BY_AGENT directly: excluded by the user's preferences before any analysis.
    S.DISCOVERED: {S.ANALYZED, S.DUPLICATE, S.FAILED, S.REJECTED_BY_AGENT},
    S.ANALYZED: {S.SHORTLISTED, S.REJECTED_BY_AGENT, S.FAILED},
    # ANALYZED again: re-analysis (e.g. after the profile changed).
    S.SHORTLISTED: {
        S.READY_TO_APPLY,
        S.BLOCKED_NEEDS_USER,
        S.ANALYZED,
        S.REJECTED_BY_AGENT,
        S.WITHDRAWN,
        S.MANUAL_APPLY,
    },
    S.READY_TO_APPLY: {
        S.APPLICATION_IN_PROGRESS,
        S.BLOCKED_NEEDS_USER,
        S.SHORTLISTED,  # draft discarded
        S.FAILED,
        S.WITHDRAWN,
        S.MANUAL_APPLY,
    },
    S.APPLICATION_IN_PROGRESS: {
        S.READY_FOR_SUBMISSION,
        S.BLOCKED_NEEDS_USER,
        S.FAILED,
        S.READY_TO_APPLY,  # browser session closed without submitting
        S.WITHDRAWN,
    },
    S.READY_FOR_SUBMISSION: {
        S.SUBMITTED,  # confirmed live submission only
        S.APPLICATION_IN_PROGRESS,  # page changed; back to filling
        S.BLOCKED_NEEDS_USER,  # challenge appeared or submission unconfirmed
        S.FAILED,
        S.READY_TO_APPLY,  # dry run finished / session closed without submitting
        S.WITHDRAWN,
    },
    S.SUBMITTED: {S.WITHDRAWN},
    # Resumes where the user unblocked it, or is abandoned.
    S.BLOCKED_NEEDS_USER: {
        S.READY_TO_APPLY,
        S.APPLICATION_IN_PROGRESS,  # only when resuming a paused browser session
        S.SUBMITTED,  # only when the USER confirms an unconfirmed live submission went through
        S.SHORTLISTED,
        S.FAILED,
        S.WITHDRAWN,
        S.MANUAL_APPLY,
    },
    # Failures can be retried from the step that failed.
    S.FAILED: {
        S.DISCOVERED,
        S.ANALYZED,
        S.SHORTLISTED,
        S.READY_TO_APPLY,
        S.REJECTED_BY_AGENT,
        S.WITHDRAWN,
        S.MANUAL_APPLY,
    },
    S.REJECTED_BY_AGENT: {S.SHORTLISTED, S.ANALYZED},  # user override / re-analysis
    # The user applies themselves: marked applied (SUBMITTED, by the user), skipped, or undone.
    S.MANUAL_APPLY: {S.SUBMITTED, S.REJECTED_BY_AGENT, S.SHORTLISTED},
    S.DUPLICATE: set(),
    S.WITHDRAWN: set(),
}


class InvalidTransitionError(ValueError):
    pass


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(f"Illegal pipeline transition {current} -> {target}")
