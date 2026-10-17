"""Featuring teachers on the public home page."""
from conftest import login


def test_featured_teacher_appears_on_home_with_designation(client, app, data):
    from app.extensions import db
    from app.models import Teacher

    with app.app_context():
        t = Teacher.query.first()
        t.show_on_website = True
        t.designation = "Senior Physics Faculty"
        db.session.commit()
        name = t.name

    body = app.test_client().get("/home").get_data(as_text=True)
    assert 'id="faculty"' in body
    assert name in body
    assert "Senior Physics Faculty" in body


def test_an_unfeatured_teacher_is_not_shown(client, app, data):
    from app.models import Teacher
    with app.app_context():
        name = Teacher.query.first().name  # left show_on_website False
    body = app.test_client().get("/home").get_data(as_text=True)
    # With nobody featured, the whole section is omitted.
    assert 'id="faculty"' not in body


def test_admin_can_feature_a_teacher_through_the_form(client, app, data):
    from app.models import Teacher

    login(client, "admin", "admin123")
    with app.app_context():
        t = Teacher.query.first()
        tid = t.id
        division_ids = [d.id for d in t.divisions]
        subject_ids = [s.id for s in t.subjects]

    client.post(f"/admin/teachers/{tid}/edit", data={
        "username": "teach1", "name": "Teacher One", "phone": "123",
        "designation": "Principal", "display_order": "1", "show_on_website": "1",
        "division_ids": division_ids, "subject_ids": subject_ids,
    }, follow_redirects=True)

    with app.app_context():
        t = Teacher.query.get(tid)
        assert t.show_on_website is True
        assert t.designation == "Principal"
        assert t.display_order == 1


def test_featured_order_respects_display_order(client, app, data):
    from app.extensions import db
    from app.models import Teacher, User

    with app.app_context():
        # Add a second teacher so ordering is observable.
        u = User(username="tz", name="Zahir Last", role="teacher")
        u.set_password("Teach@123")
        db.session.add(u)
        db.session.flush()
        db.session.add(Teacher(user_id=u.id, name="Zahir Last",
                               show_on_website=True, designation="Maths", display_order=1))
        first = Teacher.query.filter_by(name="Teacher One").first()
        first.show_on_website = True
        first.designation = "English"
        first.display_order = 50
        db.session.commit()

    body = app.test_client().get("/home").get_data(as_text=True)
    assert body.index("Zahir Last") < body.index("Teacher One")
