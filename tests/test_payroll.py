"""Payroll: salary per teacher, monthly paid/balance, payslip."""
from datetime import date
from conftest import login


def _tid(app):
    from app.models import Teacher
    with app.app_context():
        return Teacher.query.first().id


def test_set_salary_and_see_balance(client, app, data):
    from app.extensions import db
    from app.models import Teacher, TeacherPayment
    tid = _tid(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/payroll/salary/{tid}",
                data={"monthly_salary": "20000", "year": date.today().year, "month": date.today().month},
                follow_redirects=True)
    with app.app_context():
        assert Teacher.query.get(tid).monthly_salary == 20000
        db.session.add(TeacherPayment(teacher_id=tid, amount=12000, pay_date=date.today()))
        db.session.commit()

    body = client.get(f"/admin/payroll?year={date.today().year}&month={date.today().month}").get_data(as_text=True)
    assert "20,000" in body   # salary
    assert "12,000" in body   # paid
    assert "8,000" in body    # balance


def test_payroll_admin_only(client, app, data):
    login(client, "office1", "Office@123")
    assert client.get("/admin/payroll").status_code == 403


def test_payslip_pdf(client, app, data):
    tid = _tid(app)
    login(client, "admin", "admin123")
    r = client.get(f"/admin/payroll/payslip/{tid}")
    assert r.status_code == 200 and r.headers.get("Content-Type") == "application/pdf"


def test_payroll_excel(client, app, data):
    login(client, "admin", "admin123")
    r = client.get("/admin/payroll/excel")
    assert r.status_code == 200 and "spreadsheet" in r.headers.get("Content-Type", "")


def test_paid_counts_only_the_selected_month(client, app, data):
    from app.extensions import db
    from app.models import TeacherPayment
    tid = _tid(app)
    with app.app_context():
        db.session.add_all([
            TeacherPayment(teacher_id=tid, amount=5000, pay_date=date(2026, 1, 15)),
            TeacherPayment(teacher_id=tid, amount=7000, pay_date=date(2026, 2, 10)),
        ])
        db.session.commit()
    login(client, "admin", "admin123")
    jan = client.get("/admin/payroll?year=2026&month=1").get_data(as_text=True)
    assert "5,000" in jan and "7,000" not in jan
