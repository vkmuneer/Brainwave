"""Academic year: selector, stamping new transactions, filtering reports."""
from datetime import date
from conftest import login


def test_current_year_seeded_and_shown(client, app, data):
    from app.models import AcademicYear
    with app.app_context():
        y = AcademicYear.current()
        assert y is not None and y.label == "2026-2027"
    login(client, "admin", "admin123")
    assert "2026-2027" in client.get("/admin/dashboard").get_data(as_text=True)


def test_new_payment_stamped_with_active_year(client, app, data):
    from app.models import FeePayment, Student, AcademicYear
    login(client, "admin", "admin123")
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id
        yid = AcademicYear.current().id
    client.post(f"/admin/students/{sid}/payments/add",
                data={"amount": "4321", "mode": "Cash"}, follow_redirects=True)
    with app.app_context():
        p = FeePayment.query.filter_by(amount=4321).first()
        assert p is not None and p.academic_year_id == yid


def test_admin_adds_year_and_switches(client, app, data):
    from app.models import AcademicYear, FeePayment, Student
    login(client, "admin", "admin123")
    client.post("/admin/academic-years", data={"label": "2027-2028", "is_current": ""}, follow_redirects=True)
    with app.app_context():
        new_year = AcademicYear.query.filter_by(label="2027-2028").first()
        assert new_year is not None
        new_id = new_year.id
        sid = Student.query.filter_by(admission_no="S1").first().id

    # Switch the working year, then record a payment -> stamped with the new year.
    client.post("/admin/set-academic-year", data={"academic_year_id": new_id}, follow_redirects=True)
    client.post(f"/admin/students/{sid}/payments/add", data={"amount": "8765", "mode": "Cash"}, follow_redirects=True)
    with app.app_context():
        p = FeePayment.query.filter_by(amount=8765).first()
        assert p.academic_year_id == new_id


def test_fee_collection_filters_by_year(client, app, data):
    from app.models import AcademicYear, Student
    login(client, "admin", "admin123")
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id
    # Two small payments so both fit within the class base fee.
    client.post(f"/admin/students/{sid}/payments/add", data={"amount": "3210", "mode": "Cash"}, follow_redirects=True)
    # Add + switch to a new year, record another
    client.post("/admin/academic-years", data={"label": "2027-2028"}, follow_redirects=True)
    with app.app_context():
        new_id = AcademicYear.query.filter_by(label="2027-2028").first().id
        old_id = AcademicYear.query.filter_by(label="2026-2027").first().id
    client.post("/admin/set-academic-year", data={"academic_year_id": new_id}, follow_redirects=True)
    client.post(f"/admin/students/{sid}/payments/add", data={"amount": "7650", "mode": "Cash"}, follow_redirects=True)

    today = date.today().isoformat()
    # Under new year: sees 7,650 not 3,210
    b_new = client.get(f"/admin/reports/finance?period=daily&date={today}").get_data(as_text=True)
    assert "7,650" in b_new and "3,210" not in b_new
    # Switch back: sees 3,210 not 7,650
    client.post("/admin/set-academic-year", data={"academic_year_id": old_id}, follow_redirects=True)
    b_old = client.get(f"/admin/reports/finance?period=daily&date={today}").get_data(as_text=True)
    assert "3,210" in b_old and "7,650" not in b_old


def test_management_is_admin_only_selector_is_office(client, app, data):
    # Office can switch the year...
    login(client, "office1", "Office@123")
    from app.models import AcademicYear
    with app.app_context():
        yid = AcademicYear.current().id
    assert client.post("/admin/set-academic-year", data={"academic_year_id": yid}).status_code in (302, 303)
    # ...but cannot manage years.
    assert client.get("/admin/academic-years").status_code == 403
