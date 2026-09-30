"""Challenge detection, submission confirmation and URL redaction (pure functions)."""

import pytest

from integrations.browser.signals import classify_submission, detect_challenges, redact_url


def signals(**over: object) -> dict[str, object]:
    return {
        "iframes": [],
        "captcha_elements": 0,
        "password_inputs": 0,
        "otp_inputs": 0,
        "text": "",
        **over,
    }


def kinds(**over: object) -> set[str]:
    return {c.type for c in detect_challenges(signals(**over))}


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"captcha_elements": 1}, "CAPTCHA"),
        ({"iframes": ["https://www.google.com/recaptcha/api2/anchor?k=x"]}, "CAPTCHA"),
        ({"iframes": ["https://newassets.hcaptcha.com/captcha/v1"]}, "CAPTCHA"),
        ({"iframes": ["https://challenges.cloudflare.com/turnstile/v0"]}, "CAPTCHA"),
        ({"text": "Please verify you are human"}, "CAPTCHA"),
        ({"otp_inputs": 1}, "OTP"),
        ({"text": "Enter the verification code we sent to your email"}, "OTP"),
        ({"text": "Enter your one-time password"}, "OTP"),
        ({"text": "We sent a text message to your phone"}, "SMS_VERIFICATION"),
        ({"text": "Please verify your email address to continue"}, "EMAIL_VERIFICATION"),
        ({"text": "Two-factor authentication is required"}, "TWO_FACTOR"),
        ({"text": "Enter the code from your authenticator app"}, "TWO_FACTOR"),
        ({"text": "Please verify your identity"}, "IDENTITY_VERIFICATION"),
        ({"password_inputs": 1}, "LOGIN_REQUIRED"),
        ({"text": "Sign in to apply for this job"}, "LOGIN_REQUIRED"),
    ],
)
def test_challenges_are_detected(over: dict[str, object], expected: str) -> None:
    assert expected in kinds(**over)


def test_blocked_access_is_a_challenge() -> None:
    found = detect_challenges(signals(), http_status=403)
    assert [c.type for c in found] == ["ACCESS_BLOCKED"] and "does not bypass" in found[
        0
    ].user_action


def test_plain_application_page_has_no_challenge() -> None:
    text = "Apply for Backend Engineer. First name, last name, email, resume. Submit application"
    assert kinds(text=text) == set()
    assert detect_challenges(signals(text=text), http_status=200) == []


def test_every_challenge_says_what_the_user_must_do() -> None:
    for c in detect_challenges(signals(captcha_elements=1, otp_inputs=1, password_inputs=1)):
        assert c.user_action and "PIE" in c.user_action or "yourself" in c.user_action


@pytest.mark.parametrize(
    ("text", "form_left", "status", "outcome"),
    [
        ("Thank you for applying! Your application ID is APP-10001.", False, 200, "CONFIRMED"),
        ("Your application has been received.", False, 200, "CONFIRMED"),
        ("Thanks for your application. Reference number: REF-99812", True, 200, "CONFIRMED"),
        ("Careers. See our open roles.", False, 200, "AMBIGUOUS"),  # navigation alone
        ("", False, 200, "AMBIGUOUS"),  # HTTP 200 alone
        ("Thank you for applying!", True, 200, "AMBIGUOUS"),  # form still there, no id
        ("Something went wrong, please try again", False, 200, "FAILED"),
        ("Email is required", True, 200, "FAILED"),
        ("Internal error", False, 500, "FAILED"),
        ("You have already applied to this job.", False, 200, "ALREADY_APPLIED"),
    ],
)
def test_submission_needs_positive_confirmation(
    text: str, form_left: bool, status: int, outcome: str
) -> None:
    check = classify_submission(text, "https://x.test/done", status, form_still_present=form_left)
    assert check.outcome == outcome, check


def test_confirmation_extracts_application_id() -> None:
    check = classify_submission(
        "Thank you for applying! Your application ID is APP-10001.",
        "u",
        200,
        form_still_present=False,
    )
    assert check.application_id == "APP-10001" and check.excerpt


def test_redact_url_drops_secrets() -> None:
    url = "https://jobs.test/apply?gh_jid=123&token=abc&otp=999&session_id=s&x=1#frag"
    red = redact_url(url) or ""
    assert "abc" not in red and "999" not in red and "=s&" not in red and "frag" not in red
    assert "gh_jid=123" in red and "x=1" in red and red.count("REDACTED") == 3
    assert redact_url(None) is None


# --- false positives: page text alone is not a challenge ------------------------------------

DESCRIPTION = (
    "About the role: you will build login flows with 2FA, OTP delivery and captcha "
    "integrations, run our two-factor authentication service and authenticator app, and own "
    "identity verification. Strings we localise: 'Please verify you are human', 'Enter the "
    "verification code we sent', 'Sign in to apply'. "
) * 5
APPLICATION_UI = "First Name * \n Last Name * \n Email * \n Resume/CV * \n Submit application"


@pytest.mark.parametrize(
    "word", ["2FA", "OTP", "captcha", "authenticator app", "verify your identity"]
)
def test_job_description_words_do_not_trigger_a_challenge(word: str) -> None:
    text = f"{DESCRIPTION} We need someone who knows {word}."
    s = {
        "iframes": [],
        "captcha_elements": 0,
        "password_inputs": 0,
        "otp_inputs": 0,
        "ui_text": APPLICATION_UI,
        "text": text,
        "text_length": len(text),
    }
    assert detect_challenges(s) == []


def test_the_same_words_in_the_verification_ui_do_trigger() -> None:
    ui = "Two-factor authentication \n Enter the code from your authenticator app \n Verify"
    s = {
        "iframes": [],
        "captcha_elements": 0,
        "password_inputs": 0,
        "otp_inputs": 0,
        "ui_text": ui,
        "text": DESCRIPTION,
        "text_length": len(DESCRIPTION),
    }
    assert "TWO_FACTOR" in {c.type for c in detect_challenges(s)}


def test_widgets_and_inputs_trigger_regardless_of_text() -> None:
    base = {
        "iframes": [],
        "captcha_elements": 0,
        "password_inputs": 0,
        "otp_inputs": 0,
        "ui_text": APPLICATION_UI,
        "text": DESCRIPTION,
        "text_length": len(DESCRIPTION),
    }
    assert {c.type for c in detect_challenges({**base, "captcha_elements": 1})} == {"CAPTCHA"}
    assert {c.type for c in detect_challenges({**base, "otp_inputs": 1})} == {"OTP"}
    assert {c.type for c in detect_challenges({**base, "password_inputs": 1})} == {"LOGIN_REQUIRED"}


def test_short_interstitial_page_counts_as_verification_ui() -> None:
    text = "Checking your browser before accessing jobs.example. Verify you are human."
    s = {
        "iframes": [],
        "captcha_elements": 0,
        "password_inputs": 0,
        "otp_inputs": 0,
        "ui_text": "",
        "text": text,
        "text_length": len(text),
    }
    assert {c.type for c in detect_challenges(s)} == {"CAPTCHA"}
