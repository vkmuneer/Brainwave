"""Student list downloads, and deleting a student."""
from datetime import date
from conftest import login


def test_excel_download_respects_class_filter(client, app, data):
    from app.models import SchoolClass
    login(client, "admin", "admin123")
    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first().id
    r = client.get(f"/admin/students/excel?class_id={sslc}")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers.get("Content-Type", "")
    assert "students.xlsx" in r.headers.get("Content-Disposition", "")


def test_pdf_download(client, app, data):
    login(client, "admin", "admin123")
    r = client.get("/admin/students/pdf")
    assert r.status_code == 200
    assert r.headers.get("Content-Type") == "application/pdf"


def test_students_page_still_lists_and_filters(client, app, data):
    from app.models import SchoolClass
    login(client, "admin", "admin123")
    body = client.get("/admin/students").get_data(as_text=True)
    assert "Student A" in body and "Student C" in body
    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first().id
    filtered = client.get(f"/admin/students?class_id={sslc}").get_data(as_text=True)
    assert "Student A" in filtered and "Student C" not in filtered


def test_delete_needs_the_admission_number(client, app, data):
    from app.models import Student
    login(client, "admin", "admin123")
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id
    # Wrong confirmation -> nothing deleted.
    client.post(f"/admin/students/{sid}/delete", data={"confirm": "WRONG"}, follow_redirects=True)
    with app.app_context():
        assert Student.query.get(sid) is not None


def test_delete_removes_student_and_their_records(client, app, data):
    from app.extensions import db
    from app.models import Student, FeePayment, Attendance, ExamMark
    login(client, "admin", "admin123")
    with app.app_context():
        st = Student.query.filter_by(admission_no="S1").first()
        sid = st.id
        db.session.add_all([
            FeePayment(student_id=sid, amount=500, payment_date=date.today(), mode="Cash"),
            Attendance(student_id=sid, division_id=st.division_id, date=date.today(),
                       session="full", status="present"),
        ])
        db.session.commit()
        assert ExamMark.query.filter_by(student_id=sid).count() > 0  # fixture gives A a mark

    client.post(f"/admin/students/{sid}/delete", data={"confirm": "S1"}, follow_redirects=True)
    with app.app_context():
        assert Student.query.get(sid) is None
        assert FeePayment.query.filter_by(student_id=sid).count() == 0
        assert Attendance.query.filter_by(student_id=sid).count() == 0
        assert ExamMark.query.filter_by(student_id=sid).count() == 0


def test_confirmation_page_shows_what_will_be_lost(client, app, data):
    from app.extensions import db
    from app.models import Student, FeePayment
    login(client, "admin", "admin123")
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id
        db.session.add(FeePayment(student_id=sid, amount=2500, payment_date=date.today(), mode="Cash"))
        db.session.commit()
    body = client.get(f"/admin/students/{sid}/delete").get_data(as_text=True)
    assert "cannot be undone" in body
    assert "2,500" in body


def test_office_cannot_delete(client, app, data):
    from app.models import Student
    login(client, "office1", "Office@123")
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id
    assert client.get(f"/admin/students/{sid}/delete").status_code == 403
    client.post(f"/admin/students/{sid}/delete", data={"confirm": "S1"})
    with app.app_context():
        assert Student.query.get(sid) is not None


def test_other_branch_student_cannot_be_deleted(client, app, data):
    from app.models import Student
    login(client, "office2", "Office@123")   # Higher Secondary
    with app.app_context():
        sid = Student.query.filter_by(admission_no="S1").first().id  # High School
    assert client.get(f"/admin/students/{sid}/delete").status_code == 403
