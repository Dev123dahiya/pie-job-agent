"""Domain enumerations shared by models, API schemas and the agent."""

from enum import StrEnum


class JobPipelineState(StrEnum):
    """Workflow state of a job, from discovery to submission (see database/state_machine.py).

    BLOCKED_NEEDS_USER covers every case where only the user can proceed: missing
    information, CAPTCHA, OTP/2FA, login walls. The specific reason is recorded on the
    application (user_action_required) and in the timeline.
    """

    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    SHORTLISTED = "SHORTLISTED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLICATION_IN_PROGRESS = "APPLICATION_IN_PROGRESS"
    # Form filled and validated in the browser; only the final submit remains (dry run stops here).
    READY_FOR_SUBMISSION = "READY_FOR_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    # Alternate / terminal states
    REJECTED_BY_AGENT = "REJECTED_BY_AGENT"
    BLOCKED_NEEDS_USER = "BLOCKED_NEEDS_USER"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
    WITHDRAWN = "WITHDRAWN"


class ApplicationStatus(StrEnum):
    """Lifecycle status of an application, including post-submission (spec section 18)."""

    DISCOVERED = "DISCOVERED"
    SHORTLISTED = "SHORTLISTED"
    # Internal draft only: nothing has been sent to the employer.
    DRAFT = "DRAFT"
    APPLIED = "APPLIED"
    APPLICATION_CONFIRMED = "APPLICATION_CONFIRMED"
    ASSESSMENT = "ASSESSMENT"
    RECRUITER_CONTACT = "RECRUITER_CONTACT"
    INTERVIEW = "INTERVIEW"
    FINAL_ROUND = "FINAL_ROUND"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    NO_RESPONSE = "NO_RESPONSE"
    REQUIRES_USER_ACTION = "REQUIRES_USER_ACTION"


class EmailClassification(StrEnum):
    APPLICATION_CONFIRMATION = "APPLICATION_CONFIRMATION"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    RECRUITER = "RECRUITER"
    REJECTION = "REJECTION"
    SHORTLISTED = "SHORTLISTED"
    OFFER = "OFFER"
    OTHER_JOB_EMAIL = "OTHER_JOB_EMAIL"
    IRRELEVANT = "IRRELEVANT"
    UNCLASSIFIED = "UNCLASSIFIED"


class MatchLevel(StrEnum):
    """Qualitative match for one analysis dimension. UNKNOWN when the posting is silent."""

    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    """Posting age vs. the configured window. UNKNOWN when the posting has no date."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class LeadStatus(StrEnum):
    """A lead from an untrusted aggregator (Adzuna, Himalayas). A lead is never a job: only
    the trusted ATS posting it leads to can be analyzed, whatever the lead's status."""

    NEW = "NEW"  # registered, employer not resolved yet
    NO_BOARD = "NO_BOARD"  # no supported ATS board found for the employer (re-checked later)
    BOARD_CANDIDATE = "BOARD_CANDIDATE"  # a possible board, not verified: needs the user
    VERIFIED = "VERIFIED"  # the employer's board is verified; waiting to match a posting
    MATCHED = "MATCHED"  # linked to a trusted ATS job found through this lead's board
    DUPLICATE = "DUPLICATE"  # linked to a trusted ATS job PIE already had
    NO_MATCH = "NO_MATCH"  # board known, but no posting passes the strict same-job rules
    AMBIGUOUS = "AMBIGUOUS"  # several postings could be this job; none is picked
    REJECTED = "REJECTED"  # excluded company: never resolved


class BoardStatus(StrEnum):
    """An employer -> supported-ATS board mapping learned from leads."""

    CANDIDATE = "CANDIDATE"  # the board exists but its identity is not proven
    VERIFIED = "VERIFIED"  # identity proven (board metadata, configured board, or the user)
    REJECTED = "REJECTED"  # the user rejected it
    NOT_FOUND = "NOT_FOUND"  # no board found for the company (negative cache)


class RemoteType(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class EventSource(StrEnum):
    AGENT = "agent"
    EMAIL = "email"
    USER = "user"
    SYSTEM = "system"
    BROWSER = "browser"


class AgentRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"  # cancellation requested; the worker stops at the next safe point
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"  # paused until the user answers; resumable
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class AgentActionStatus(StrEnum):
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    DRY_RUN = "DRY_RUN"  # intended action logged in development mode, not executed


class RunType(StrEnum):
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    AGENT = "agent"  # autonomous Claude agent run (tool-using loop)
    DAILY = "daily"  # daily discovery + analysis; parent of its discovery/analysis runs
    MOCK = "mock"


class JobLogAction(StrEnum):
    """Action codes written to agent_actions.tool_name for the discovery/analysis audit log."""

    DISCOVERY_STARTED = "DISCOVERY_STARTED"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    SEARCH_FAILED = "SEARCH_FAILED"
    JOB_DISCOVERED = "JOB_DISCOVERED"
    JOB_NORMALIZED = "JOB_NORMALIZED"
    JOB_DUPLICATE = "JOB_DUPLICATE"
    JOB_FAILED = "JOB_FAILED"
    DISCOVERY_COMPLETED = "DISCOVERY_COMPLETED"
    JOB_ANALYSIS_STARTED = "JOB_ANALYSIS_STARTED"
    JOB_ANALYSIS_COMPLETED = "JOB_ANALYSIS_COMPLETED"
    JOB_ANALYSIS_FAILED = "JOB_ANALYSIS_FAILED"
    JOB_SHORTLISTED = "JOB_SHORTLISTED"
    JOB_REJECTED = "JOB_REJECTED"
    RUN_CANCELLED = "RUN_CANCELLED"
    # A job stored without a post date whose own source later published one.
    JOB_POST_DATE_LEARNED = "JOB_POST_DATE_LEARNED"
    DAILY_RUN_STARTED = "DAILY_RUN_STARTED"
    DAILY_STEP_COMPLETED = "DAILY_STEP_COMPLETED"
    DAILY_STEP_FAILED = "DAILY_STEP_FAILED"
    DAILY_RUN_COMPLETED = "DAILY_RUN_COMPLETED"
    # Lead -> trusted ATS resolution (Phase 5.7).
    LEADS_REGISTERED = "LEADS_REGISTERED"
    BOARD_RESOLVED = "BOARD_RESOLVED"
    LEAD_MATCHED = "LEAD_MATCHED"
    JOB_SUPERSEDED = "JOB_SUPERSEDED"  # an aggregator job row linked to its trusted ATS job


class AgentEvent(StrEnum):
    """Structured events of an autonomous agent run (agent_actions.tool_name).

    Only concise summaries and tool inputs/outputs are stored - never hidden reasoning.
    """

    RUN_STARTED = "RUN_STARTED"
    RUN_RESUMED = "RUN_RESUMED"
    USER_MESSAGE = "USER_MESSAGE"
    THINKING = "THINKING"  # the agent's one-line summary of its chosen next step
    TOOL_CALL_STARTED = "TOOL_CALL_STARTED"
    TOOL_CALL_COMPLETED = "TOOL_CALL_COMPLETED"
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    STATE_CHANGED = "STATE_CHANGED"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"


class ToolPermission(StrEnum):
    READ_ONLY = "READ_ONLY"  # reads PIE state; no side effects
    WRITE = "WRITE"  # changes PIE's own database/files only
    # Touches the outside world (employer websites). Runs only in an external execution mode.
    EXTERNAL_ACTION = "EXTERNAL_ACTION"


class ExecutionMode(StrEnum):
    """What a run / request may do. External work defaults to EXTERNAL_DRY_RUN; EXTERNAL_LIVE
    also needs EXTERNAL_LIVE_ENABLED=true and a per-application user confirmation."""

    READ_ONLY = "read_only"
    INTERNAL_WRITES = "internal_writes"
    EXTERNAL_DRY_RUN = "external_dry_run"
    EXTERNAL_LIVE = "external_live"


class BrowserSessionState(StrEnum):
    CREATED = "CREATED"
    OPEN = "OPEN"
    NAVIGATING = "NAVIGATING"
    FORM_DETECTED = "FORM_DETECTED"
    FILLING = "FILLING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    READY_FOR_SUBMISSION = "READY_FOR_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class BrowserExecutionMode(StrEnum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class AnswerStatus(StrEnum):
    """Only VERIFIED answers are ever entered automatically."""

    VERIFIED = "VERIFIED"
    UNCONFIRMED = "UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


class ExternalActionResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUSED = "REFUSED"  # a gate stopped it before anything external happened
    BLOCKED = "BLOCKED"  # needs the user (CAPTCHA, OTP, login, unknown question...)
    DRY_RUN = "DRY_RUN"  # dry run: the irreversible step was not performed


class SubmissionOutcome(StrEnum):
    PENDING = "PENDING"  # recorded before the click; a crash leaves it here (never retried)
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    FAILED = "FAILED"
    USER_CONFIRMED = "USER_CONFIRMED"
    USER_NOT_SUBMITTED = "USER_NOT_SUBMITTED"


class ResumeValidationStatus(StrEnum):
    """MASTER: the immutable source file. VALIDATED: a tailored version that passed every
    grounding check. Tailored output that fails validation is never stored as a version."""

    MASTER = "MASTER"
    VALIDATED = "VALIDATED"
