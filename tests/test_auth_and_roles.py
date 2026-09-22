"""Who can sign in, and what each role may reach."""
from conftest import login


def test_anonymous_lands_on_the_public_site(client):
    response = client.get("/", follow_redirects=True)
    assert b"Brainwave Academy" in response.data


def test_admin_can_sign_in(client, data):
    assert login(client, "admin", "admin123").status_code in (302, 200)


def test_wrong_password_is_refused(client, data):
    response = client.post(
        "/login", data={"username": "office1", "password": "nope"}, follow_redirects=True
    )
    assert b"Invalid username or password" in response.data


def test_deactivated_account_cannot_sign_in(client, app, data):
    from app.extensions import db
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(username="office1").first()
        user.active = False
        db.session.commit()

    response = client.post(
        "/login", data={"username": "office1", "password": "Office@123"}, follow_redirects=True
    )
    assert b"Invalid username or password" in response.data


def test_repeated_failures_lock_the_account(client, data):
    for _ in range(9):
        response = client.post(
            "/login", data={"username": "office1", "password": "wrong"}, follow_redirects=True
        )
    assert b"Too many failed attempts" in response.data


def test_next_parameter_cannot_leave_the_site(client, data):
    response = client.post(
        "/login?next=https://evil.example.com/steal",
        data={"username": "office1", "password": "Office@123"},
    )
    assert "evil.example.com" not in response.headers.get("Location", "")


def test_teacher_cannot_reach_admin_pages(client, data):
    login(client, "teach1", "Teach@123")
    assert client.get("/admin/students").status_code == 403


def test_office_cannot_reach_admin_only_pages(client, data):
    login(client, "office1", "Office@123")
    for url in ["/admin/settings", "/admin/teachers", "/admin/classes", "/admin/audit-log"]:
        assert client.get(url).status_code == 403, url


def test_office_can_reach_its_own_work(client, data):
    login(client, "office1", "Office@123")
    for url in ["/admin/students", "/admin/attendance/mark", "/admin/reports/pending-fees"]:
        assert client.get(url).status_code == 200, url


def test_forbidden_page_is_readable(client, data):
    login(client, "teach1", "Teach@123")
    response = client.get("/admin/students")
    assert b"Not available to your login" in response.data
    assert b"read-protected" not in response.data


def test_signed_out_user_is_sent_to_login(client):
    response = client.get("/admin/students")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
