"""The public site, CSRF, and how the app behaves when things go wrong."""
import pytest

from conftest import login


@pytest.fixture()
def csrf_app(app):
    """CSRF is switched off for most tests; this fixture turns it back on."""
    app.config.update(WTF_CSRF_ENABLED=True)
    return app


def test_post_without_a_csrf_token_is_rejected(csrf_app, data):
    client = csrf_app.test_client()
    response = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert response.status_code == 400


def test_login_works_with_a_token(csrf_app, data):
    import re

    client = csrf_app.test_client()
    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    response = client.post(
        "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"}
    )
    assert response.status_code == 302


def test_every_form_carries_a_token(app):
    """A form without a token would break the moment it is submitted."""
    import pathlib
    import re

    missing = []
    for path in pathlib.Path("app/templates").rglob("*.html"):
        body = path.read_text()
        for match in re.finditer(r"<form\b[^>]*?>", body, re.I | re.S):
            tag = match.group(0)
            if 'method="post"' not in tag.lower():
                continue
            following = body[match.end():match.end() + 260]
            if "csrf_token" not in following:
                missing.append(f"{path}: {tag[:60]}")
    assert not missing, missing


def test_public_home_lists_the_classes(client, data):
    body = client.get("/home").get_data(as_text=True)
    assert "SSLC" in body and "Classes we coach" in body


def test_public_home_hides_blank_contact_details(client, app, data):
    body = client.get("/home").get_data(as_text=True)
    assert "Contact details will be published" in body


def test_public_home_shows_contact_details_once_set(client, app, data):
    from app.extensions import db
    from app.models import Settings

    with app.app_context():
        settings = Settings.get()
        settings.address = "Mukkam Road, Omassery"
        settings.phone = "+91 98470 00000"
        db.session.commit()

    body = client.get("/home").get_data(as_text=True)
    assert "Mukkam Road, Omassery" in body
    assert "Contact details will be published" not in body


def test_missing_page_is_friendly(client, data):
    response = client.get("/admin/nope")
    assert response.status_code == 404
    assert b"Page not found" in response.data


def test_logo_falls_back_when_none_uploaded(client, data):
    response = client.get("/logo")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/")


def test_secret_key_placeholder_is_refused(monkeypatch):
    """A published key would let anyone forge an admin session."""
    import importlib

    monkeypatch.setenv("SECRET_KEY", "change-this-to-a-long-random-string")
    monkeypatch.delenv("ALLOW_DEFAULT_SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)

    import config

    importlib.reload(config)
    from app import create_app

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(config.Config)


def test_student_cannot_be_given_a_duplicate_admission_number(client, app, data):
    from app.models import Student

    login(client, "admin", "admin123")
    client.post(
        "/admin/students/new",
        data={
            "admission_no": "S1",
            "name": "Impostor",
            "division_id": data["sslc_div_id"],
            "parent_whatsapp": "9000000009",
        },
        follow_redirects=True,
    )
    with app.app_context():
        assert Student.query.filter_by(admission_no="S1").count() == 1
