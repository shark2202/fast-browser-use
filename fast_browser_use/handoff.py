"""Harness-triggered handoff detector.

Small, explicit, overridable heuristics over the observed page dict (no model
call). Returns a reason string when the page looks like a blocking auth step,
else None. The agent calls this during observe/prepare; the model never selects
handoff (zero-hallucination preserved). BLOCKED-status handoff is decided by the
agent loop (3 repeated no-change actions), not here.
"""
import re

_LOGIN_URL = re.compile(r"/(?:login|signin|sign-in|auth)\b", re.I)
_LOGIN_TEXT = re.compile(r"\b(sign in|log in|sign-in|login)\b", re.I)
_PASSWORD_LABEL = re.compile(r"password", re.I)
_SUBMIT_LABEL = re.compile(r"sign in|log in|sign-in|login|continue|submit", re.I)
_CAPTCHA = re.compile(r"recaptcha|hcaptcha|captcha", re.I)
_2FA = re.compile(r"verification code|two-factor|2fa|otp|authenticator|security code", re.I)
_IDP_URL = re.compile(
    r"accounts\.google\.com|login\.microsoftonline|github\.com/login|auth0|okta|saml|sso", re.I
)


def detect_handoff(page, history=None):
    """Return a handoff reason string for blocking auth pages, or None.

    Priority: captcha > 2fa > login > auth-redirect (more specific blockers first).
    """
    text = page.get("text", "") or ""
    url = page.get("url", "") or ""
    actions = page.get("actions", []) or []
    labels = " ".join(a.get("label", "") for a in actions)

    if _CAPTCHA.search(text) or _CAPTCHA.search(labels):
        return "captcha-detected"
    if _2FA.search(text) or _2FA.search(labels):
        return "2fa-required"

    password_field = any(
        a.get("kind") == "fill" and _PASSWORD_LABEL.search(a.get("label", "")) for a in actions
    )
    has_submit = any(
        a.get("kind") == "click" and _SUBMIT_LABEL.search(a.get("label", "")) for a in actions
    )
    has_fill = any(a.get("kind") == "fill" for a in actions)
    login_signals = bool(_LOGIN_TEXT.search(text) or _LOGIN_URL.search(url))
    if (password_field and has_submit) or (login_signals and has_fill and has_submit):
        return "login-required"

    if _IDP_URL.search(url):
        return "auth-redirect"
    return None
