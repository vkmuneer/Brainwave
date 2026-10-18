"""Recording cash payments to teachers."""
from datetime import date
from conftest import login


def _teacher_id(app):
    from app.models import Teacher
    with app.app_context():
        return Teacher.query.first().id


def test_office_can_record_a_payment(client, app, data):
    from app.models import TeacherPayment
    tid = _teacher_id(app)
    login(client, "office1", "Office@123")
    client.post("/admin/teacher-payments",
                data={"teacher_id": tid, "amount": "5000", "pay_date": date.today().isoformat(),
                      "purpose": "October salary", "period": "Oct 2026", "mode": "Cash"},
                follow_redirects=True)
    with app.app_context():
        p = TeacherPayment.query.first()
        assert p is not None and p.amount == 5000
        assert p.purpose == "October salary"
        assert p.paid_by == "Office One"


def test_admin_can_record_and_it_lists(client, app, data):
    tid = _teacher_id(app)
    login(client, "admin", "admin123")
    client.post("/admin/teacher-payments",
                data={"teacher_id": tid, "amount": "3000", "purpose": "extra classes"},
                follow_redirects=True)
    body = client.get("/admin/teacher-payments").get_data(as_text=True)
    assert "extra classes" in body
    assert "3,000" in body


def test_zero_amount_refused(client, app, data):
    from app.models import TeacherPayment
    tid = _teacher_id(app)
    login(client, "admin", "admin123")
    client.post("/admin/teacher-payments", data={"teacher_id": tid, "amount": "0"}, follow_redirects=True)
    with app.app_context():
        assert TeacherPayment.query.count() == 0


def test_excel_download(client, app, data):
    tid = _teacher_id(app)
    login(client, "admin", "admin123")
    client.post("/admin/teacher-payments", data={"teacher_id": tid, "amount": "1000"}, follow_redirects=True)
    r = client.get("/admin/teacher-payments/excel")
    assert r.status_code == 200 and "spreadsheet" in r.headers.get("Content-Type", "")


def test_only_admin_can_delete(client, app, data):
    from app.models import TeacherPayment
    tid = _teacher_id(app)
    login(client, "admin", "admin123")
    client.post("/admin/teacher-payments", data={"teacher_id": tid, "amount": "1000"}, follow_redirects=True)
    with app.app_context():
        pid = TeacherPayment.query.first().id

    office = app.test_client(); login(office, "office1", "Office@123")
    assert office.post(f"/admin/teacher-payments/{pid}/delete").status_code == 403

    client.post(f"/admin/teacher-payments/{pid}/delete", follow_redirects=True)
    with app.app_context():
        assert TeacherPayment.query.count() == 0


def test_teacher_cannot_access(client, app, data):
    login(client, "teach1", "Teach@123")
    assert client.get("/admin/teacher-payments").status_code == 403
