"""Hourly remuneration for teachers, and hours-based payment."""
from conftest import login


def _tid(app):
    from app.models import Teacher
    with app.app_context():
        return Teacher.query.first().id


def test_office_can_set_hourly_rate(client, app, data):
    from app.models import Teacher
    tid = _tid(app)
    login(client, "office1", "Office@123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "500"}, follow_redirects=True)
    with app.app_context():
        assert Teacher.query.get(tid).hourly_rate == 500


def test_admin_can_set_hourly_rate(client, app, data):
    from app.models import Teacher
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "750"}, follow_redirects=True)
    with app.app_context():
        assert Teacher.query.get(tid).hourly_rate == 750


def test_negative_rate_refused(client, app, data):
    from app.models import Teacher
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "-5"}, follow_redirects=True)
    with app.app_context():
        assert Teacher.query.get(tid).hourly_rate == 0


def test_office_sees_teachers_page_with_rate_field(client, app, data):
    login(client, "office1", "Office@123")
    body = client.get("/admin/teachers").get_data(as_text=True)
    assert body.count("Hourly remuneration") >= 1
    # Office must not get the admin-only actions.
    assert "Add Teacher" not in body
    assert "Deactivate" not in body


def test_payment_records_hours_and_rate(client, app, data):
    from app.models import TeacherPayment, Teacher
    tid = _tid(app)
    login(client, "office1", "Office@123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "400"}, follow_redirects=True)
    # 12 hours at 400 = 4800 (the page computes it; the amount is what's posted)
    client.post("/admin/teacher-payments",
                data={"teacher_id": tid, "amount": "4800", "hours": "12", "purpose": "October classes"},
                follow_redirects=True)
    with app.app_context():
        p = TeacherPayment.query.first()
        assert p.amount == 4800
        assert p.hours == 12
        assert p.hourly_rate == 400      # snapshot of the rate used


def test_rate_change_does_not_rewrite_past_payments(client, app, data):
    from app.models import TeacherPayment
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "400"}, follow_redirects=True)
    client.post("/admin/teacher-payments", data={"teacher_id": tid, "amount": "4800", "hours": "12"},
                follow_redirects=True)
    # Raise the rate afterwards.
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "600"}, follow_redirects=True)
    with app.app_context():
        p = TeacherPayment.query.first()
        assert p.hourly_rate == 400 and p.amount == 4800


def test_amount_without_hours_still_works(client, app, data):
    from app.models import TeacherPayment
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post("/admin/teacher-payments", data={"teacher_id": tid, "amount": "9000"}, follow_redirects=True)
    with app.app_context():
        p = TeacherPayment.query.first()
        assert p.amount == 9000 and p.hours is None and p.hourly_rate is None


def test_teacher_cannot_set_rates(client, app, data):
    tid = _tid(app)
    login(client, "teach1", "Teach@123")
    assert client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "999"}).status_code == 403


def test_calculator_script_is_in_the_body_not_the_title(client, app, data):
    """The hours-to-amount script must render in the page body.

    It was once injected into {% block title %}, so it sat inside <title> and
    never ran - the amount silently stopped auto-filling.
    """
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "400"}, follow_redirects=True)

    html = client.get("/admin/teacher-payments").get_data(as_text=True)
    head, _, body = html.partition("</head>")
    assert "calcPay" not in head, "the calculator script leaked into <head>/<title>"
    assert "calcPay" in body
    assert 'id="payHours"' in body and 'id="payAmount"' in body


def test_teacher_rate_reaches_the_page_for_the_calculator(client, app, data):
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/teachers/{tid}/hourly-rate", data={"hourly_rate": "450"}, follow_redirects=True)
    body = client.get("/admin/teacher-payments").get_data(as_text=True)
    assert 'data-rate="450' in body, "the hourly rate must be on the option for the calculator"
