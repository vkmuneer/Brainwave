"""The paid video course platform: access must follow the subscription."""
from datetime import date, timedelta

from conftest import login


def _make_course(app, price=1000, days=30):
    from app.extensions import db
    from app.models import Course, CourseVideo

    with app.app_context():
        course = Course(title="Test Course", price=price, duration_days=days, published=True)
        db.session.add(course)
        db.session.flush()
        video = CourseVideo(course_id=course.id, title="Lesson 1",
                            url="https://youtu.be/abc12345", sequence=1)
        db.session.add(video)
        db.session.commit()
        return course.id, video.id


def _register(client, mobile="9111111111", password="Pass@1234"):
    return client.post(
        "/learn/register",
        data={"name": "A Subscriber", "mobile": mobile, "password": password},
        follow_redirects=False,
    )


def test_catalogue_is_public(client, app, data):
    _make_course(app)
    assert b"Test Course" in client.get("/learn/").data


def test_draft_courses_are_not_public(client, app, data):
    from app.extensions import db
    from app.models import Course

    with app.app_context():
        db.session.add(Course(title="Draft Course", price=500, published=False))
        db.session.commit()
    assert b"Draft Course" not in client.get("/learn/").data


def test_registration_creates_an_account(client, app, data):
    _register(client)
    from app.models import Subscriber

    with app.app_context():
        assert Subscriber.query.count() == 1


def test_short_password_is_refused(client, app, data):
    client.post(
        "/learn/register",
        data={"name": "X", "mobile": "9222222222", "password": "abc"},
        follow_redirects=True,
    )
    from app.models import Subscriber

    with app.app_context():
        assert Subscriber.query.count() == 0


def test_unpaid_subscriber_cannot_watch(client, app, data):
    course_id, video_id = _make_course(app)
    _register(client)
    response = client.get(f"/learn/course/{course_id}/watch/{video_id}")
    assert response.status_code == 302


def test_activation_opens_access(client, app, data):
    from app.extensions import db
    from app.models import Subscription

    course_id, video_id = _make_course(app)
    _register(client)
    client.post(f"/learn/course/{course_id}/subscribe")

    with app.app_context():
        subscription = Subscription.query.first()
        assert subscription.status == "pending"
        subscription_id = subscription.id

    staff = app.test_client()
    login(staff, "office1", "Office@123")
    staff.post(f"/admin/subscriptions/{subscription_id}/decide", data={"decision": "activate"})

    assert client.get(f"/learn/course/{course_id}/watch/{video_id}").status_code == 200


def test_expired_subscription_is_refused(client, app, data):
    from app.extensions import db
    from app.models import Subscription

    course_id, video_id = _make_course(app)
    _register(client)
    client.post(f"/learn/course/{course_id}/subscribe")

    with app.app_context():
        subscription = Subscription.query.first()
        subscription.status = "active"
        subscription.starts_on = date.today() - timedelta(days=60)
        subscription.expires_on = date.today() - timedelta(days=1)
        db.session.commit()

    assert client.get(f"/learn/course/{course_id}/watch/{video_id}").status_code == 302


def test_access_runs_from_activation_not_request(client, app, data):
    from app.models import Subscription

    course_id, _ = _make_course(app, days=30)
    _register(client)
    client.post(f"/learn/course/{course_id}/subscribe")

    with app.app_context():
        subscription_id = Subscription.query.first().id

    staff = app.test_client()
    login(staff, "office1", "Office@123")
    staff.post(f"/admin/subscriptions/{subscription_id}/decide", data={"decision": "activate"})

    with app.app_context():
        subscription = Subscription.query.get(subscription_id)
        assert subscription.starts_on == date.today()
        assert subscription.expires_on == date.today() + timedelta(days=30)


def test_one_subscriber_cannot_open_anothers_payment_page(client, app, data):
    from app.models import Subscription

    course_id, _ = _make_course(app)
    _register(client, mobile="9333333333")
    client.post(f"/learn/course/{course_id}/subscribe")
    with app.app_context():
        subscription_id = Subscription.query.first().id

    other = app.test_client()
    _register(other, mobile="9444444444")
    assert other.get(f"/learn/subscription/{subscription_id}/payment").status_code == 403


def test_staff_session_cannot_watch_without_subscribing(client, app, data):
    course_id, video_id = _make_course(app)
    staff = app.test_client()
    login(staff, "admin", "admin123")
    assert staff.get(f"/learn/course/{course_id}/watch/{video_id}").status_code == 302


def test_subscriber_cannot_reach_staff_pages(client, app, data):
    _register(client)
    assert client.get("/admin/students").status_code == 302
