"""Fees: what a student owes, what may be recorded, and the installment plan."""
from datetime import date

from conftest import login


def test_fee_comes_from_the_class_not_the_student(app, data):
    from app.models import Student, SchoolClass

    with app.app_context():
        student = Student.query.get(data["student_a"])
        sslc = SchoolClass.query.get(data["sslc_id"])
        assert student.class_fee == sslc.base_fee


def test_discount_reduces_what_is_payable(app, data):
    from app.extensions import db
    from app.models import Student

    with app.app_context():
        student = Student.query.get(data["student_a"])
        full = student.total_fee
        student.discount_amount = 1000
        db.session.commit()
        assert student.total_fee == full - 1000
        assert student.pending_fee == full - 1000


def test_payment_cannot_exceed_the_balance(client, app, data):
    from app.models import Student

    login(client, "office1", "Office@123")
    with app.app_context():
        pending = Student.query.get(data["student_a"]).pending_fee

    client.post(
        f"/admin/students/{data['student_a']}/payments/add",
        data={"amount": pending + 5000, "payment_date": date.today().isoformat(), "mode": "Cash"},
        follow_redirects=True,
    )
    with app.app_context():
        assert Student.query.get(data["student_a"]).total_paid <= pending


def test_a_valid_payment_is_recorded_with_a_receipt(client, app, data):
    from app.models import Student

    login(client, "office1", "Office@123")
    client.post(
        f"/admin/students/{data['student_a']}/payments/add",
        data={"amount": 500, "payment_date": date.today().isoformat(), "mode": "Cash"},
        follow_redirects=True,
    )
    with app.app_context():
        student = Student.query.get(data["student_a"])
        assert student.total_paid == 500
        assert student.payments[0].receipt_no.startswith("BW")
        assert student.payments[0].recorded_by == "Office One"


def test_installment_plan_matches_the_published_schedule(app, data):
    from app.models import SchoolClass

    with app.app_context():
        sslc = SchoolClass.query.get(data["sslc_id"])
        cap = sslc.base_fee
        # 5,000 by end of May, then 1,000 a month, capped at the full fee.
        assert sslc.scheduled_due(date(2026, 5, 31), cap) == 5000
        assert sslc.scheduled_due(date(2026, 6, 30), cap) == 6000
        assert sslc.scheduled_due(date(2026, 12, 31), cap) == 12000
        assert sslc.scheduled_due(date(2027, 1, 31), cap) == 12500


def test_schedule_never_asks_for_more_than_is_owed(app, data):
    from app.models import SchoolClass

    with app.app_context():
        sslc = SchoolClass.query.get(data["sslc_id"])
        discounted_cap = 8000
        assert sslc.scheduled_due(date(2027, 1, 31), discounted_cap) == 8000


def test_class9_plan_completes_in_december(app, data):
    from app.models import SchoolClass

    with app.app_context():
        nine = SchoolClass.query.filter_by(name="9").first()
        assert nine.scheduled_due(date(2026, 12, 31), nine.base_fee) == nine.base_fee


def test_pending_fees_report_excludes_the_other_branch(client, app, data):
    from app.models import Student

    login(client, "office1", "Office@123")
    response = client.get("/admin/reports/pending-fees")
    body = response.get_data(as_text=True)
    with app.app_context():
        assert Student.query.get(data["student_a"]).name in body
        assert Student.query.get(data["student_c"]).name not in body
