"""Timetable: division grid editor, teacher view, clash detection."""
from conftest import login


def _ids(app):
    from app.models import Division, Subject, Teacher
    with app.app_context():
        div = Division.query.first()
        subj = Subject.query.first()
        teacher = Teacher.query.first()
        return div.id, subj.id, teacher.id


def test_save_and_view_division_timetable(client, app, data):
    from app.models import TimetableSlot
    div_id, subj_id, teacher_id = _ids(app)
    login(client, "admin", "admin123")
    # Monday (0) period 1 -> subject + teacher
    client.post("/admin/timetable",
                data={"division_id": div_id, "s_0_1": subj_id, "t_0_1": teacher_id},
                follow_redirects=True)
    with app.app_context():
        sl = TimetableSlot.query.filter_by(division_id=div_id, day=0, period=1).first()
        assert sl is not None and sl.subject_id == subj_id and sl.teacher_id == teacher_id

    body = client.get(f"/admin/timetable?division_id={div_id}").get_data(as_text=True)
    assert "Save timetable" in body


def test_clearing_a_cell_deletes_the_slot(client, app, data):
    from app.models import TimetableSlot
    div_id, subj_id, teacher_id = _ids(app)
    login(client, "admin", "admin123")
    client.post("/admin/timetable", data={"division_id": div_id, "s_0_1": subj_id, "t_0_1": teacher_id}, follow_redirects=True)
    # Save again with the cell blank -> slot removed.
    client.post("/admin/timetable", data={"division_id": div_id}, follow_redirects=True)
    with app.app_context():
        assert TimetableSlot.query.filter_by(division_id=div_id, day=0, period=1).first() is None


def test_teacher_double_booking_warns(client, app, data):
    from app.extensions import db
    from app.models import Division, Subject, Teacher, SchoolClass
    login(client, "admin", "admin123")
    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first()
        div1 = sslc.divisions[0]
        div2 = Division(name="B", class_id=sslc.id)
        db.session.add(div2); db.session.commit()
        subj_id = Subject.query.first().id
        teacher_id = Teacher.query.first().id
        d1, d2 = div1.id, div2.id
    # Book teacher Mon P1 in division 1, then same slot in division 2 -> warning.
    client.post("/admin/timetable", data={"division_id": d1, "s_0_1": subj_id, "t_0_1": teacher_id}, follow_redirects=True)
    resp = client.post("/admin/timetable", data={"division_id": d2, "s_0_1": subj_id, "t_0_1": teacher_id}, follow_redirects=True)
    assert "clash" in resp.get_data(as_text=True).lower()


def test_teacher_sees_own_timetable(client, app, data):
    from app.extensions import db
    from app.models import TimetableSlot, Teacher
    with app.app_context():
        t = Teacher.query.filter_by(name="Teacher One").first()
        div_id = t.divisions[0].id
        db.session.add(TimetableSlot(division_id=div_id, day=0, period=1, teacher_id=t.id))
        db.session.commit()
    login(client, "teach1", "Teach@123")
    body = client.get("/teacher/timetable").get_data(as_text=True)
    assert "My Timetable" in body
    assert "SSLC" in body


def test_office_scoped_division_only(client, app, data):
    # office2 (Higher Secondary) cannot edit a High School division timetable.
    from app.models import SchoolClass
    login(client, "office2", "Office@123")
    with app.app_context():
        hs_div = SchoolClass.query.filter_by(name="SSLC").first().divisions[0].id
    assert client.get(f"/admin/timetable?division_id={hs_div}").status_code == 403


def test_sunday_is_available_and_saves(client, app, data):
    from app.models import TimetableSlot
    div_id, subj_id, teacher_id = _ids(app)
    login(client, "admin", "admin123")
    page = client.get(f"/admin/timetable?division_id={div_id}").get_data(as_text=True)
    assert "Sunday" in page
    # Sunday is day index 6.
    client.post("/admin/timetable",
                data={"division_id": div_id, "s_6_1": subj_id, "t_6_1": teacher_id},
                follow_redirects=True)
    with app.app_context():
        assert TimetableSlot.query.filter_by(division_id=div_id, day=6, period=1).first() is not None
