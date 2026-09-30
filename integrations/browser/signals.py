"""Pure, deterministic page analysis: challenges (CAPTCHA, OTP, 2FA, login...), submission
confirmation, and URL redaction. No browser needed, so all of it is unit-tested directly.

Detection errs on the side of stopping: a false positive only pauses for the user; a false
negative could mean interacting with a challenge, which PIE never does.
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --- challenges ---------------------------------------------------------------------------

_CAPTCHA_SRC = re.compile(r"recaptcha|hcaptcha|turnstile|arkoselabs|funcaptcha|captcha", re.I)
_CHALLENGE_TEXT: list[tuple[str, re.Pattern[str], str]] = [
    (
        "CAPTCHA",
        re.compile(
            r"verify (that )?you are (a )?human|i'?m not a robot|complete the (security )?captcha"
            r"|checking (if the site connection is secure|your browser)|are you a robot",
            re.I,
        ),
        "Complete the CAPTCHA / human check yourself in the browser, then resume.",
    ),
    (
        "TWO_FACTOR",
        re.compile(
            r"two[- ](factor|step) (authentication|verification)|authenticator app"
            r"|2fa code|enter the code from your (authenticator|app)",
            re.I,
        ),
        "Complete two-factor authentication yourself, then resume.",
    ),
    (
        "OTP",
        re.compile(
            r"one[- ]time (password|passcode|code)|enter the (\d[- ]digit )?(verification|security"
            r"|one[- ]time)? ?code|code (we|we've|we have) sent|\botp\b",
            re.I,
        ),
        "Enter the one-time code yourself (PIE never reads or guesses codes), then resume.",
    ),
    (
        "SMS_VERIFICATION",
        re.compile(r"(sent|send) (a|an|the)? ?(text message|sms)|verify your (phone|mobile)", re.I),
        "Complete the SMS verification yourself, then resume.",
    ),
    (
        "EMAIL_VERIFICATION",
        re.compile(
            r"verify your email|check your (email|inbox) for (a|the) (link|code)"
            r"|confirm your email address",
            re.I,
        ),
        "Complete the email verification yourself, then resume.",
    ),
    (
        "IDENTITY_VERIFICATION",
        re.compile(
            r"verify your identity|identity verification|upload (a|your) (government|photo)[- ]"
            r"?id|selfie verification",
            re.I,
        ),
        "Complete the identity verification yourself, then resume.",
    ),
    (
        "LOGIN_REQUIRED",
        re.compile(
            r"(sign|log) ?in to (apply|continue)|please (sign|log) ?in|create an account to apply",
            re.I,
        ),
        "Sign in yourself (PIE never handles passwords), then resume.",
    ),
]


@dataclass(frozen=True)
class Challenge:
    type: str
    detail: str
    user_action: str

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "detail": self.detail, "user_action": self.user_action}


# A page this short is itself the interactive UI (e.g. a "checking your browser" interstitial).
SHORT_PAGE_CHARS = 800


def verification_text(signals: dict[str, Any]) -> str:
    """Text a user-verification step could be written in: labels, buttons, dialogs and forms
    around visible controls - NOT the job description. A posting that merely mentions "2FA",
    "OTP" or "captcha" is not a challenge."""
    ui = signals.get("ui_text")
    body = signals.get("text") or ""
    length = signals.get("text_length", len(body))
    if ui is None:  # snapshot without interactive-text extraction: only trust a short page
        return body if length <= SHORT_PAGE_CHARS else ""
    return ui + (" \n " + body if length <= SHORT_PAGE_CHARS else "")


def detect_challenges(signals: dict[str, Any], http_status: int | None = None) -> list[Challenge]:
    """Everything on the page that only the user may handle. Never solved, never bypassed."""
    found: dict[str, Challenge] = {}

    def add(kind: str, detail: str, action: str) -> None:
        found.setdefault(kind, Challenge(kind, detail[:200], action))

    captcha_action = _CHALLENGE_TEXT[0][2]
    if signals.get("captcha_elements"):
        add("CAPTCHA", "CAPTCHA widget on the page", captcha_action)
    for src in signals.get("iframes") or []:
        if _CAPTCHA_SRC.search(src or ""):
            add("CAPTCHA", f"CAPTCHA frame ({urlsplit(src).netloc})", captcha_action)
    if signals.get("otp_inputs"):
        add("OTP", "One-time-code input on the page", _CHALLENGE_TEXT[2][2])
    if signals.get("password_inputs"):
        add(
            "LOGIN_REQUIRED",
            "Password field on the page",
            "Sign in yourself (PIE never handles passwords), then resume.",
        )
    for kind, pattern, action in _CHALLENGE_TEXT:
        if m := pattern.search(verification_text(signals)):
            add(kind, f"Verification UI says: '{m.group(0)}'", action)
    if http_status in (401, 403, 429):
        add(
            "ACCESS_BLOCKED",
            f"The site answered HTTP {http_status}",
            "The site refused automated access; apply manually. PIE does not bypass it.",
        )
    return list(found.values())


# --- submission confirmation -----------------------------------------------------------

_CONFIRMED = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|application (has been |was )?"
    r"(received|submitted)|we('ve| have) received your application|successfully (submitted|applied)"
    r"|your application is complete",
    re.I,
)
_APPLICATION_ID = re.compile(
    r"(application|reference|confirmation|submission) (id|number|no\.?|#)\s*(?:is\s+)?[:#]?\s*"
    r"([A-Z0-9][A-Z0-9-]{3,})",
    re.I,
)
_ALREADY = re.compile(r"already (applied|submitted)|duplicate application", re.I)
_ERROR = re.compile(
    r"there (was|were) (an )?(error|problem)s?|something went wrong|please (correct|fix)"
    r"|(is|are) required|could not be (submitted|processed)|submission failed|try again",
    re.I,
)


@dataclass
class SubmissionCheck:
    outcome: str  # CONFIRMED | AMBIGUOUS | FAILED | ALREADY_APPLIED
    signals: list[str] = field(default_factory=list)
    application_id: str | None = None
    excerpt: str | None = None


def classify_submission(
    text: str, url: str, http_status: int | None, *, form_still_present: bool
) -> SubmissionCheck:
    """Positive confirmation only. A click, HTTP 200 or a navigation alone is NOT evidence."""
    signals: list[str] = []
    confirmed = _CONFIRMED.search(text)
    app_id = _APPLICATION_ID.search(text)
    if confirmed:
        signals.append(f"success message: '{confirmed.group(0)}'")
    if app_id:
        signals.append(f"application id: {app_id.group(3)}")
    excerpt = None
    if confirmed or app_id:
        m = confirmed or app_id
        assert m is not None
        excerpt = text[max(0, m.start() - 80) : m.end() + 120]
    if _ALREADY.search(text):
        return SubmissionCheck("ALREADY_APPLIED", ["employer says already applied"], None, excerpt)
    error = _ERROR.search(text)
    if error and not confirmed:
        return SubmissionCheck("FAILED", [f"error message: '{error.group(0)}'"], None, excerpt)
    if http_status is not None and http_status >= 400:
        return SubmissionCheck("FAILED", [f"HTTP {http_status}"], None, excerpt)
    if confirmed and not form_still_present:
        return SubmissionCheck("CONFIRMED", signals, app_id.group(3) if app_id else None, excerpt)
    if confirmed and app_id:  # success text AND an application id, even if a form lingers
        return SubmissionCheck("CONFIRMED", signals, app_id.group(3), excerpt)
    return SubmissionCheck("AMBIGUOUS", signals or ["no positive confirmation"], None, excerpt)


# --- redaction ---------------------------------------------------------------------------

_SENSITIVE_PARAM = re.compile(
    r"token|code|otp|session|sid|auth|key|pass|secret|signature|sig|jwt|nonce|ticket", re.I
)


def redact_url(url: str | None) -> str | None:
    """Drop the values of query parameters that may carry credentials or one-time codes."""
    if not url:
        return url
    parts = urlsplit(url)
    query = [
        (k, "REDACTED" if _SENSITIVE_PARAM.search(k) else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="/:"), "")
    )  # fragment dropped


def domain_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()
