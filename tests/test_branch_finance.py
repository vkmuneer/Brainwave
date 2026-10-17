"""High School and Higher Secondary finances stay apart."""
from datetime import date

from conftest import login
import re


def _today_collection(body):
    """The rupee figure shown in the dashboard's Today's Collection tile."""
    m = re.search(r"Today's Collection</div>\s*<div class=\"stat-value\">\u20b9([\d,]+)", body)
    return m.group(1) if m else None


def _seed_payments(app):
    """A payment in each branch, dated today."""
    from app.extensions import db
    from app.models import Student, FeePayment

    with app.app_context():
        hs = Student.query.filter_by(admission_no="S1").first()     # High School (SSLC)
        hss = Student.query.filter_by(admission_no="S3").first()    # Higher Secondary (+2)
        db.session.add_all([
            FeePayment(student_id=hs.id, amount=3000, payment_date=date.today(),
                       mode="Cash", receipt_no="HS-1", recorded_by="t"),
            FeePayment(student_id=hss.id, amount=7000, payment_date=date.today(),
                       mode="UPI", receipt_no="HSS-1", recorded_by="t"),
        ])
        db.session.commit()
        return hs.name, hss.name


def test_office_sees_only_their_branch_collection(client, app, data):
    hs_name, hss_name = _seed_payments(app)

    # High School coordinator
    login(client, "office1", "Office@123")
    body = client.get("/admin/reports/finance", follow_redirects=True).get_data(as_text=True)
    assert "HS-1" in body
    assert "HSS-1" not in body
    assert hss_name not in body


def test_other_office_sees_only_the_other_branch(client, app, data):
    _seed_payments(app)
    login(client, "office2", "Office@123")
    body = client.get("/admin/reports/finance", follow_redirects=True).get_data(as_text=True)
    assert "HSS-1" in body
    assert "HS-1" not in body


def test_admin_can_filter_finance_to_one_branch(client, app, data):
    _seed_payments(app)
    login(client, "admin", "admin123")

    # Unfiltered: admin sees both receipts.
    allb = client.get("/admin/reports/finance").get_data(as_text=True)
    assert "HS-1" in allb and "HSS-1" in allb

    from app.models import Branch
    with app.app_context():
        high_id = Branch.query.filter_by(name="High School").first().id

    one = client.get(f"/admin/reports/finance?branch={high_id}").get_data(as_text=True)
    assert "HS-1" in one
    assert "HSS-1" not in one


def test_dashboard_today_collection_is_branch_scoped(client, app, data):
    _seed_payments(app)

    # Office (High School) dashboard's Today's Collection shows only its own
    # 3,000, not the academy-wide 10,000.
    login(client, "office1", "Office@123")
    body = client.get("/admin/dashboard").get_data(as_text=True)
    assert _today_collection(body) == "3,000"


def test_admin_dashboard_branch_filter_narrows_collection(client, app, data):
    _seed_payments(app)
    login(client, "admin", "admin123")
    from app.models import Branch
    with app.app_context():
        high_id = Branch.query.filter_by(name="High School").first().id

    # All branches: 3,000 + 7,000 = 10,000.
    allb = client.get("/admin/dashboard").get_data(as_text=True)
    assert _today_collection(allb) == "10,000"
    # High School only: 3,000.
    one = client.get(f"/admin/dashboard?branch={high_id}").get_data(as_text=True)
    assert _today_collection(one) == "3,000"
