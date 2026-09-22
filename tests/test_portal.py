"""The parent portal: a family sees their own child, and nobody else's."""
from datetime import date, timedelta

from conftest import portal_login, login


def test_parent_can_sign_in_with_their_number(client, data):
    assert portal_login(client, "9000000001").status_code == 302


def test_number_formats_are_all_accepted(client, data):
    for typed in ["9000000001", "+919000000001", "919000000001", " 9000000001 "]:
        client.get("/portal/logout")
        assert portal_login(client, typed).status_code == 302, typed


def test_unknown_number_is_refused(client, data):
    response = client.post(
        "/portal/login",
        data={"username": "9999999999", "password": "9999999999"},
        follow_redirects=True,
    )
    assert b"could not find that mobile number" in response.data


def test_wrong_password_is_refused(client, data):
    response = client.post(
        "/portal/login",
        data={"username": "9000000001", "password": "1234567890"},
        follow_redirects=True,
    )
    assert b"could not find that mobile number" in response.data


def test_parent_sees_only_their_own_child(client, app, data):
    portal_login(client, "9000000001")
    body = client.get("/portal/").get_data(as_text=True)
    assert "Student A" in body and "Student B" not in body


def test_another_familys_student_id_is_refused(client, data):
    portal_login(client, "9000000001")
    assert client.get(f"/portal/?student_id={data['student_b']}").status_code == 403


def test_another_familys_exam_is_refused(client, app, data):
    from app.extensions import db
    from app.models import Exam

    with app.app_context():
        other = Exam(name="Other class exam", class_id=data["plus2_id"], exam_date=date.today())
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    portal_login(client, "9000000001")
    assert client.get(f"/portal/marks/{other_id}").status_code == 404
    assert client.get(f"/portal/marks/{other_id}/report-card").status_code == 404


def test_signing_in_as_a_different_number_switches_family(client, data):
    portal_login(client, "9000000001")
    portal_login(client, "9000000002")
    body = client.get("/portal/").get_data(as_text=True)
    assert "Student B" in body and "Student A" not in body


def test_portal_can_be_switched_off(client, app, data):
    from app.extensions import db
    from app.models import Settings

    portal_login(client, "9000000001")
    with app.app_context():
        Settings.get().student_login_enabled = False
        db.session.commit()

    assert client.get("/portal/").status_code == 403


def test_anonymous_cannot_reach_portal_pages(client, data):
    for url in ["/portal/", "/portal/attendance", "/portal/marks", "/portal/videos"]:
        assert client.get(url).status_code == 302, url


def test_unapproved_video_is_hidden_from_students(client, app, data):
    from app.extensions import db
    from app.models import VideoClass

    with app.app_context():
        pending = VideoClass(
            title="Waiting approval", class_id=data["sslc_id"],
            url="https://youtu.be/abc12345", approval="pending",
            uploaded_by_name="T",
        )
        approved = VideoClass(
            title="Live lecture", class_id=data["sslc_id"],
            url="https://youtu.be/xyz98765", approval="approved",
            uploaded_by_name="T",
        )
        db.session.add_all([pending, approved])
        db.session.commit()

    portal_login(client, "9000000001")
    body = client.get("/portal/videos").get_data(as_text=True)
    assert "Live lecture" in body
    assert "Waiting approval" not in body


def test_parent_feedback_reaches_the_office(client, app, data):
    from app.models import Feedback

    portal_login(client, "9000000001")
    client.post("/portal/feedback", data={"subject": "Fees", "message": "Can we pay late?"})

    with app.app_context():
        row = Feedback.query.first()
        assert row is not None
        assert row.status == "open"
        assert row.student_id == data["student_a"]
