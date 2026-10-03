"""The user's six statuses (REQUIREMENTS 11 / 19.1), mapped from PIE's detailed pipeline states.

The dashboard shows these everywhere, with the detailed state underneath. One mapping, here,
so every page and endpoint agrees.
"""

from typing import Literal

from database.enums import JobPipelineState as JS

UserStatus = Literal["found", "skipped", "applied", "needs_input", "manual", "failed"]
USER_STATUSES: tuple[UserStatus, ...] = (
    "found",
    "skipped",
    "applied",
    "needs_input",
    "manual",
    "failed",
)

_BY_STATE: dict[JS, UserStatus] = {
    JS.DISCOVERED: "found",
    JS.ANALYZED: "found",
    JS.SHORTLISTED: "found",
    JS.READY_TO_APPLY: "found",
    JS.APPLICATION_IN_PROGRESS: "found",
    JS.READY_FOR_SUBMISSION: "found",
    JS.SUBMITTED: "applied",
    JS.REJECTED_BY_AGENT: "skipped",
    JS.DUPLICATE: "skipped",
    JS.WITHDRAWN: "skipped",
    JS.BLOCKED_NEEDS_USER: "needs_input",
    JS.FAILED: "failed",
}


def user_status(state: JS) -> UserStatus:
    return _BY_STATE[state]


def states_for(status: UserStatus) -> list[JS]:
    return [s for s, u in _BY_STATE.items() if u == status]
