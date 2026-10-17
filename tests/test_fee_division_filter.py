"""Division filter across the fee-management pages."""
from conftest import login


def _two_divisions(app):
    """Add a second SSLC division with its own student; return (class_id, divA, divB)."""
    from app.extensions import db
    from app.models import SchoolClass, Division, Student, FeePayment
    from datetime import date

    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first()
        div_a = sslc.divisions[0]
        div_b = Division(name="B", class_id=sslc.id)
        db.session.add(div_b)
        db.session.flush()
        z = Student(admission_no="S9", name="Zoya Second", class_id=sslc.id,
                    division_id=div_b.id, parent_whatsapp="9000000099", parent_name="P Z")
        db.session.add(z)
        db.session.commit()
        # Student A (div A) has no payment -> pending; Zoya (div B) also pending.
        return sslc.id, div_a.id, div_b.id


def test_pending_fees_filters_by_division(client, app, data):
    class_id, a, b = _two_divisions(app)
    login(client, "admin", "admin123")
    body = client.get(f"/admin/reports/pending-fees?class_id={class_id}&division_id={b}").get_data(as_text=True)
    assert "Zoya Second" in body
    assert "Student A" not in body


def test_student_wise_filters_by_division(client, app, data):
    class_id, a, b = _two_divisions(app)
    login(client, "admin", "admin123")
    body = client.get(f"/admin/reports/student-wise?class_id={class_id}&division_id={a}").get_data(as_text=True)
    assert "Student A" in body
    assert "Zoya Second" not in body


def test_bulk_payment_sheet_filters_by_division(client, app, data):
    class_id, a, b = _two_divisions(app)
    login(client, "admin", "admin123")
    # The sheet download should succeed with a division filter applied.
    resp = client.get(f"/admin/payments/bulk-upload/template?class_id={class_id}&division_id={b}")
    assert resp.status_code == 200
    assert "spreadsheet" in resp.headers.get("Content-Type", "")


def test_the_pages_render_the_division_dropdown(client, app, data):
    login(client, "admin", "admin123")
    for url in ("/admin/students", "/admin/reports/pending-fees",
                "/admin/reports/student-wise", "/admin/payments/bulk-upload"):
        body = client.get(url).get_data(as_text=True)
        assert 'name="division_id"' in body, url
