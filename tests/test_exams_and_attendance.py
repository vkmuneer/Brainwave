"""Exam results, ranking, and the attendance register."""
from datetime import date

from conftest import login


def test_rank_is_a_position_in_the_class(app, data):
    from app.models import Exam, Student
    from app.utils.exam_analysis import compute_exam_results

    with app.app_context():
        exam = Exam.query.get(data["exam_id"])
        students = Student.query.filter_by(class_id=data["sslc_id"]).all()
        results = compute_exam_results(exam, students)
        ranked = {r["student"].name: r["rank"] for r in results if r["complete"]}
        assert ranked["Student A"] == 1
        assert ranked["Student B"] == 2


def test_progress_page_ranks_against_classmates(client, app, data):
    """A student ranked alone would always come first."""
    login(client, "admin", "admin123")
    body = client.get(f"/admin/students/{data['student_b']}/progress").get_data(as_text=True)
    assert "Rank in class" in body
    # Student B scored 40 against Student A's 90, so cannot be first.
    import re
    section = body.split("Rank in class")[1][:200]
    assert ">1<" not in section


def test_a_half_marked_exam_is_not_reported_as_failure(app, data):
    from app.extensions import db
    from app.models import Exam, ExamSubject, Subject, Student
    from app.utils.exam_analysis import student_progress

    with app.app_context():
        # Add a second subject with no marks entered, making the exam incomplete.
        extra = Subject(name="Science")
        db.session.add(extra)
        db.session.flush()
        db.session.add(
            ExamSubject(exam_id=data["exam_id"], subject_id=extra.id, max_marks=100, pass_marks=35)
        )
        db.session.commit()

        student = Student.query.get(data["student_a"])
        progress = student_progress(student, Exam.query.filter_by(class_id=data["sslc_id"]).all())
        assert progress is None


def test_marks_above_the_maximum_are_rejected(client, app, data):
    from app.models import ExamMark

    login(client, "admin", "admin123")
    client.post(
        f"/admin/exams/{data['exam_id']}/marks",
        data={
            "division_id": data["sslc_div_id"],
            "exam_subject_id": data["exam_subject_id"],
            f"marks_{data['student_a']}": "150",
        },
        follow_redirects=True,
    )
    with app.app_context():
        mark = ExamMark.query.filter_by(
            exam_subject_id=data["exam_subject_id"], student_id=data["student_a"]
        ).first()
        assert mark.marks_obtained <= 100


def test_attendance_records_one_row_per_session(client, app, data):
    from app.models import Attendance

    login(client, "office1", "Office@123")
    client.post(
        "/admin/attendance/mark",
        data={
            "division_id": data["sslc_div_id"],
            "att_date": "2026-09-01",
            "session": "full",
            f"present_{data['student_a']}": "on",
        },
        follow_redirects=True,
    )
    with app.app_context():
        rows = Attendance.query.filter_by(date=date(2026, 9, 1)).all()
        by_student = {r.student_id: r.status for r in rows}
        assert by_student[data["student_a"]] == "present"
        assert by_student[data["student_b"]] == "absent"


def test_twice_daily_keeps_the_two_sessions_apart(client, app, data):
    from app.extensions import db
    from app.models import Attendance, Settings

    with app.app_context():
        Settings.get().attendance_sessions = "twice"
        db.session.commit()

    login(client, "office1", "Office@123")
    for session, present in (("fn", True), ("an", False)):
        payload = {
            "division_id": data["sslc_div_id"],
            "att_date": "2026-09-02",
            "session": session,
        }
        if present:
            payload[f"present_{data['student_a']}"] = "on"
        client.post("/admin/attendance/mark", data=payload, follow_redirects=True)

    with app.app_context():
        rows = Attendance.query.filter_by(
            date=date(2026, 9, 2), student_id=data["student_a"]
        ).all()
        assert {r.session: r.status for r in rows} == {"fn": "present", "an": "absent"}


def test_single_mode_ignores_a_stale_session_parameter(client, app, data):
    from app.models import Attendance, Settings

    login(client, "office1", "Office@123")
    client.post(
        "/admin/attendance/mark",
        data={
            "division_id": data["sslc_div_id"],
            "att_date": "2026-09-03",
            "session": "fn",
        },
        follow_redirects=True,
    )
    with app.app_context():
        rows = Attendance.query.filter_by(date=date(2026, 9, 3)).all()
        assert rows and all(r.session == "full" for r in rows)
