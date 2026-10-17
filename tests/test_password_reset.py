"""Self-service password reset by email link."""
import re

from conftest import login


def _configure_mail(app):
    app.config.update(MAIL_USERNAME="x@gmail.com", MAIL_PASSWORD="secret", MAIL_FROM="x@gmail.com")


def test_valid_token_resets_the_password(client, app, data):
    from app.auth.routes import _make_reset_token
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        token = _make_reset_token(user)

    # GET shows the form, POST sets the new password.
    assert client.get(f"/reset-password/{token}").status_code == 200
    client.post(f"/reset-password/{token}",
                data={"new_password": "BrandNew@1", "confirm_password": "BrandNew@1"},
                follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(username="admin").first().check_password("BrandNew@1")


def test_a_tampered_token_is_refused(client, app, data):
    from app.models import User
    resp = client.get("/reset-password/not-a-real-token", follow_redirects=True)
    assert "invalid or has expired" in resp.get_data(as_text=True)
    with app.app_context():
        # Password unchanged (still the seeded one).
        assert User.query.filter_by(username="admin").first().check_password("admin123")


def test_an_expired_token_is_refused(client, app, data, monkeypatch):
    import app.auth.routes as routes
    from app.models import User

    monkeypatch.setattr(routes, "RESET_MAX_AGE", -1)  # already expired
    with app.app_context():
        token = routes._make_reset_token(User.query.filter_by(username="admin").first())
    resp = client.get(f"/reset-password/{token}", follow_redirects=True)
    assert "invalid or has expired" in resp.get_data(as_text=True)


def test_short_or_mismatched_passwords_are_refused(client, app, data):
    from app.auth.routes import _make_reset_token
    from app.models import User
    with app.app_context():
        token = _make_reset_token(User.query.filter_by(username="admin").first())

    client.post(f"/reset-password/{token}", data={"new_password": "short", "confirm_password": "short"})
    client.post(f"/reset-password/{token}", data={"new_password": "LongEnough1", "confirm_password": "Different2"})
    with app.app_context():
        assert User.query.filter_by(username="admin").first().check_password("admin123")


def test_forgot_password_emails_a_working_link(client, app, data, monkeypatch):
    from app.extensions import db
    from app.models import User
    import app.auth.routes as routes

    _configure_mail(app)
    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        user.email = "principal@brainwave.in"
        db.session.commit()

    sent = {}
    def fake_send(to, subject, body):
        sent.update(to=to, subject=subject, body=body)
        return {"status": "sent"}
    monkeypatch.setattr(routes, "send_email", fake_send)

    client.post("/forgot-password", data={"username": "admin"}, follow_redirects=True)
    assert sent.get("to") == "principal@brainwave.in"

    link = re.search(r"(/reset-password/[^\s]+)", sent["body"]).group(1)
    client.post(link, data={"new_password": "FreshPass9", "confirm_password": "FreshPass9"},
                follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(username="admin").first().check_password("FreshPass9")


def test_forgot_without_email_falls_back_to_office_request(client, app, data, monkeypatch):
    from app.models import User, PasswordResetRequest
    import app.auth.routes as routes

    _configure_mail(app)  # mail is on, but this user has no email
    monkeypatch.setattr(routes, "send_email", lambda *a, **k: {"status": "sent"})

    with app.app_context():
        assert User.query.filter_by(username="office1").first().email is None

    client.post("/forgot-password", data={"username": "office1"}, follow_redirects=True)
    with app.app_context():
        uid = User.query.filter_by(username="office1").first().id
        assert PasswordResetRequest.query.filter_by(user_id=uid, status="pending").first() is not None


def test_forgot_password_gives_nothing_away_for_unknown_user(client, data):
    resp = client.post("/forgot-password", data={"username": "nobody"}, follow_redirects=True)
    assert "if that account exists" in resp.get_data(as_text=True)
