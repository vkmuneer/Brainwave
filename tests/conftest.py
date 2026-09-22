"""Shared fixtures.

Every test gets a fresh temporary database, so a test can create, break or
delete anything without touching the academy's real records.
"""
import os
import tempfile
from datetime import date, timedelta

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-a-placeholder")


@pytest.fixture()
def app():
    handle, db_path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    import config
    import importlib

    importlib.reload(config)
    from app import create_app

    application = create_app(config.Config)
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    yield application

    os.unlink(db_path)


@pytest.fixture(autouse=True)
def _clear_lockouts():
    """Failed-login counters live in module state for the life of the process,
    so one test's lockout would otherwise lock the account for later tests."""
    from app.auth import routes as auth_routes
    from app.portal import routes as portal_routes
    from app.learn import routes as learn_routes

    for module in (auth_routes, portal_routes, learn_routes):
        module._failed_attempts.clear()
    yield


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    from app.extensions import db as _db

    with app.app_context():
        yield _db


@pytest.fixture()
def data(app):
    """A small academy: two branches, classes, staff, students, an exam."""
    from app.extensions import db
    from app.models import (
        Branch, SchoolClass, Division, User, Teacher, Student,
        Subject, Exam, ExamSubject, ExamMark, Settings,
    )

    with app.app_context():
        high = Branch.query.filter_by(name="High School").first()
        higher = Branch.query.filter_by(name="Higher Secondary").first()

        sslc = SchoolClass.query.filter_by(name="SSLC").first()
        plus2 = SchoolClass.query.filter_by(name="+2").first()
        sslc_div = sslc.divisions[0]
        plus2_div = plus2.divisions[0]

        office = User(username="office1", name="Office One", role="office", branch_id=high.id)
        office.set_password("Office@123")
        other_office = User(
            username="office2", name="Office Two", role="office", branch_id=higher.id
        )
        other_office.set_password("Office@123")

        teacher_user = User(username="teach1", name="Teacher One", role="teacher")
        teacher_user.set_password("Teach@123")
        db.session.add_all([office, other_office, teacher_user])
        db.session.flush()

        teacher = Teacher(user_id=teacher_user.id, name="Teacher One")
        teacher.divisions = [sslc_div]
        db.session.add(teacher)

        a = Student(
            admission_no="S1", name="Student A", class_id=sslc.id, division_id=sslc_div.id,
            parent_whatsapp="9000000001", parent_name="Parent A",
        )
        b = Student(
            admission_no="S2", name="Student B", class_id=sslc.id, division_id=sslc_div.id,
            parent_whatsapp="9000000002", parent_name="Parent B",
        )
        c = Student(
            admission_no="S3", name="Student C", class_id=plus2.id, division_id=plus2_div.id,
            parent_whatsapp="9000000003", parent_name="Parent C",
        )
        db.session.add_all([a, b, c])

        maths = Subject.query.filter_by(name="Maths").first() or Subject(name="Maths")
        db.session.add(maths)
        db.session.flush()

        exam = Exam(name="Term 1", class_id=sslc.id, exam_date=date.today())
        db.session.add(exam)
        db.session.flush()
        exam_subject = ExamSubject(exam_id=exam.id, subject_id=maths.id, max_marks=100, pass_marks=35)
        db.session.add(exam_subject)
        db.session.flush()
        db.session.add_all([
            ExamMark(exam_subject_id=exam_subject.id, student_id=a.id, marks_obtained=90),
            ExamMark(exam_subject_id=exam_subject.id, student_id=b.id, marks_obtained=40),
        ])

        settings = Settings.get()
        settings.student_login_enabled = True
        settings.upi_id = "test@upi"
        db.session.commit()

        return {
            "sslc_id": sslc.id, "plus2_id": plus2.id,
            "sslc_div_id": sslc_div.id, "plus2_div_id": plus2_div.id,
            "student_a": a.id, "student_b": b.id, "student_c": c.id,
            "exam_id": exam.id, "exam_subject_id": exam_subject.id,
            "high_branch": high.id, "higher_branch": higher.id,
        }


def login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def portal_login(client, mobile):
    return client.post(
        "/portal/login", data={"username": mobile, "password": mobile}, follow_redirects=False
    )
