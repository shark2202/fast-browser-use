from fast_browser_use.handoff import detect_handoff


def _page(url="https://x/", text="", actions=None, dialogs=None):
    return {"url": url, "text": text, "actions": actions or [], "dialogs": dialogs or []}


def test_login_wall_detected():
    page = _page(
        url="https://app.example.com/login",
        text="Sign in to your account",
        actions=[
            {"node": 1, "role": "textbox", "label": "Email", "kind": "fill", "value": "", "id": "e1"},
            {"node": 2, "role": "textbox", "label": "Password", "kind": "fill", "value": "", "id": "e2"},
            {"node": 3, "role": "button", "label": "Sign in", "kind": "click", "value": "", "id": "e3"},
        ],
    )
    assert detect_handoff(page) == "login-required"


def test_captcha_detected():
    page = _page(text="Please complete the reCAPTCHA challenge")
    assert detect_handoff(page) == "captcha-detected"


def test_2fa_detected():
    page = _page(
        text="Enter your verification code",
        actions=[{"node": 1, "role": "textbox", "label": "Code", "kind": "fill", "value": "", "id": "e1"}],
    )
    assert detect_handoff(page) == "2fa-required"


def test_auth_redirect_to_idp_detected():
    page = _page(url="https://accounts.google.com/o/oauth2/auth?client_id=x")
    assert detect_handoff(page) == "auth-redirect"


def test_no_handoff_for_normal_page():
    page = _page(
        url="https://example.com/",
        text="Example Domain",
        actions=[{"node": 1, "role": "link", "label": "More information", "kind": "click", "value": "", "id": "e1"}],
    )
    assert detect_handoff(page) is None


def test_captcha_takes_priority_over_login():
    page = _page(
        url="https://app.example.com/login",
        text="Sign in and complete the reCAPTCHA",
        actions=[
            {"node": 1, "role": "textbox", "label": "Password", "kind": "fill", "value": "", "id": "e1"},
            {"node": 2, "role": "button", "label": "Sign in", "kind": "click", "value": "", "id": "e2"},
        ],
    )
    assert detect_handoff(page) == "captcha-detected"
