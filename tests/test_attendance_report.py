"""Period attendance reports: student-wise, division-wise, downloadable."""
from datetime import date, timedelta
from conftest import login


def _seed_attendance(app):
    from app.extensions import db
    from app.models import Student, Attendance
    with app.app_context():
        a = Student.query.filter_by(admission_no="S1").first()
        b = Student.query.filter_by(admission_no="S2").first()
        d0 = date.today()
        rows = [
            Attendance(student_id=a.id, division_id=a.division_id, date=d0, session="full", status="present"),
            Attendance(student_id=a.id, division_id=a.division_id, date=d0 - timedelta(days=1), session="full", status="absent"),
            Attendance(student_id=b.id, division_id=b.division_id, date=d0, session="full", status="present"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        return a.division_id


def test_student_wise_report_shows_counts(client, app, data):
    _seed_attendance(app)
    login(client, "admin", "admin123")
    start = (date.today() - timedelta(days=3)).isoformat()
    end = date.today().isoformat()
    body = client.get(f"/admin/reports/attendance-summary?view=student&start={start}&end={end}").get_data(as_text=True)
    assert "Student A" in body
    assert "50.0%" in body  # A: 1 present / 2 marked


def test_division_wise_report(client, app, data):
    _seed_attendance(app)
    login(client, "admin", "admin123")
    start = (date.today() - timedelta(days=3)).isoformat()
    end = date.today().isoformat()
    body = client.get(f"/admin/reports/attendance-summary?view=division&start={start}&end={end}").get_data(as_text=True)
    assert "Class-Division" in body


def test_excel_download(client, app, data):
    _seed_attendance(app)
    login(client, "admin", "admin123")
    r = client.get("/admin/reports/attendance-summary/excel?view=student")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers.get("Content-Type", "")


def test_pdf_download(client, app, data):
    _seed_attendance(app)
    login(client, "admin", "admin123")
    r = client.get("/admin/reports/attendance-summary/pdf?view=division")
    assert r.status_code == 200
    assert r.headers.get("Content-Type") == "application/pdf"


def test_office_scoped_to_their_branch(client, app, data):
    _seed_attendance(app)
    # office2 is Higher Secondary; A and B are High School -> should not appear.
    login(client, "office2", "Office@123")
    body = client.get("/admin/reports/attendance-summary?view=student").get_data(as_text=True)
    assert "Student A" not in body
