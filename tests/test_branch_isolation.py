"""One coordinator must not reach the other branch's records."""
from conftest import login


def test_each_coordinator_sees_only_their_own_students(client, app, data):
    from app.models import Student

    with app.app_context():
        mine = Student.query.get(data["student_a"]).name
        theirs = Student.query.get(data["student_c"]).name

    login(client, "office1", "Office@123")
    body = client.get("/admin/students").get_data(as_text=True)
    assert mine in body and theirs not in body

    client.get("/logout")
    login(client, "office2", "Office@123")
    body = client.get("/admin/students").get_data(as_text=True)
    assert theirs in body and mine not in body


def test_other_branch_student_cannot_be_opened_by_id(client, data):
    login(client, "office1", "Office@123")
    for url in [
        f"/admin/students/{data['student_c']}",
        f"/admin/students/{data['student_c']}/edit",
        f"/admin/students/{data['student_c']}/statement/pdf",
        f"/admin/students/{data['student_c']}/progress",
    ]:
        assert client.get(url).status_code == 403, url


def test_other_branch_division_register_is_refused(client, data):
    login(client, "office1", "Office@123")
    response = client.get(f"/admin/attendance/mark?division_id={data['plus2_div_id']}")
    assert response.status_code == 403


def test_admin_sees_every_branch(client, app, data):
    from app.models import Student

    with app.app_context():
        names = [
            Student.query.get(data["student_a"]).name,
            Student.query.get(data["student_c"]).name,
        ]
    login(client, "admin", "admin123")
    body = client.get("/admin/students").get_data(as_text=True)
    assert all(n in body for n in names)


def test_broadcast_cannot_target_the_other_branch(client, data):
    login(client, "office1", "Office@123")
    response = client.post(
        "/admin/broadcast",
        data={"kind": "custom", "scope": "class", "class_id": data["plus2_id"], "message": "hi"},
    )
    assert response.status_code == 403


def test_other_branch_exam_is_refused(client, app, data):
    from app.extensions import db
    from app.models import Exam
    from datetime import date

    with app.app_context():
        other = Exam(name="Their exam", class_id=data["plus2_id"], exam_date=date.today())
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    login(client, "office1", "Office@123")
    assert client.get(f"/admin/exams/{other_id}").status_code == 403
    assert client.get(f"/admin/exams/{other_id}/marks/template").status_code == 403
