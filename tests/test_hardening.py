"""Transport and browser-level protections, checked on real responses."""
import io

import pytest

from conftest import login


@pytest.mark.parametrize(
    "header,expected",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "SAMEORIGIN"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ],
)
def test_security_headers_are_sent(client, data, header, expected):
    assert client.get("/home").headers.get(header) == expected


def test_content_security_policy_limits_sources(client, data):
    csp = client.get("/home").headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    # The players the app legitimately embeds are allowed, nothing else.
    assert "youtube-nocookie.com" in csp and "player.vimeo.com" in csp


def test_headers_are_on_signed_in_pages_too(client, data):
    login(client, "admin", "admin123")
    response = client.get("/admin/dashboard")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in response.headers


def test_session_cookie_is_httponly_and_samesite(client, data):
    response = login(client, "admin", "admin123")
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_secure_cookies_switch_on_for_https(monkeypatch):
    import importlib

    monkeypatch.setenv("SECURE_COOKIES", "1")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-a-placeholder")
    import config

    importlib.reload(config)
    assert config.Config.SESSION_COOKIE_SECURE is True
    assert config.Config.REMEMBER_COOKIE_SECURE is True

    monkeypatch.setenv("SECURE_COOKIES", "0")
    importlib.reload(config)
    assert config.Config.SESSION_COOKIE_SECURE is False


def test_oversized_upload_is_refused(client, app, data):
    app.config["MAX_CONTENT_LENGTH"] = 1024
    login(client, "admin", "admin123")
    response = client.post(
        "/admin/settings",
        data={"academy_name": "X", "logo": (io.BytesIO(b"x" * 5000), "big.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


def test_no_template_bypasses_escaping(app):
    """A single |safe on user-supplied text would undo Jinja's escaping."""
    import pathlib

    offenders = [
        str(p) for p in pathlib.Path("app/templates").rglob("*.html")
        if "|safe" in p.read_text() or "| safe" in p.read_text()
    ]
    assert not offenders, offenders


def test_no_raw_sql_built_from_user_input(app):
    """Raw SQL is fine for migrations; interpolating a request value is not."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        body = path.read_text()
        for match in re.finditer(r"text\(\s*f[\"']", body):
            snippet = body[match.start():match.start() + 200]
            if "request." in snippet or "form" in snippet:
                offenders.append(f"{path}: {snippet[:70]}")
    assert not offenders, offenders


def test_student_name_with_html_is_escaped(client, app, data):
    from app.extensions import db
    from app.models import Student

    with app.app_context():
        student = Student.query.get(data["student_a"])
        student.name = "<script>alert('x')</script>"
        db.session.commit()

    login(client, "admin", "admin123")
    body = client.get("/admin/students").get_data(as_text=True)
    assert "<script>alert('x')</script>" not in body
    assert "&lt;script&gt;" in body


def test_short_passwords_are_refused_everywhere(client, app, data):
    """One constant drives every place a password is set."""
    from config import MIN_PASSWORD_LENGTH
    from app.models import User

    assert MIN_PASSWORD_LENGTH >= 8

    login(client, "admin", "admin123")
    client.post(
        "/admin/office-staff",
        data={"name": "Weak", "username": "weakuser", "password": "abc",
              "branch_id": data["high_branch"]},
        follow_redirects=True,
    )
    with app.app_context():
        assert User.query.filter_by(username="weakuser").first() is None


def test_changing_to_a_short_password_is_refused(client, app, data):
    from app.models import User

    login(client, "office1", "Office@123")
    client.post(
        "/account/change-password",
        data={"current_password": "Office@123", "new_password": "abc", "confirm_password": "abc"},
        follow_redirects=True,
    )
    with app.app_context():
        assert User.query.filter_by(username="office1").first().check_password("Office@123")
