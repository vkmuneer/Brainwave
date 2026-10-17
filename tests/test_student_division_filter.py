"""Division dropdown filter on the students page."""
from conftest import login


def test_division_filter_narrows_the_list(client, app, data):
    from app.models import SchoolClass, Division, Student

    login(client, "admin", "admin123")
    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first()
        div_a = sslc.divisions[0]
        # A second division in SSLC, with its own student, to prove exclusion.
        div_b = Division(name="B", class_id=sslc.id)
        from app.extensions import db
        db.session.add(div_b)
        db.session.flush()
        db.session.add(Student(admission_no="S9", name="Zoya Second", class_id=sslc.id,
                               division_id=div_b.id, parent_whatsapp="9000000099",
                               parent_name="Parent Z"))
        db.session.commit()
        class_id, a_id, b_id = sslc.id, div_a.id, div_b.id

    # Filter to division A: Student A/B present, the division-B student absent.
    body = client.get(f"/admin/students?class_id={class_id}&division_id={a_id}").get_data(as_text=True)
    assert "Student A" in body
    assert "Zoya Second" not in body

    # Filter to division B: only the division-B student.
    body = client.get(f"/admin/students?class_id={class_id}&division_id={b_id}").get_data(as_text=True)
    assert "Zoya Second" in body
    assert "Student A" not in body


def test_division_dropdown_lists_the_selected_class_divisions(client, app, data):
    from app.models import SchoolClass
    login(client, "admin", "admin123")
    with app.app_context():
        sslc = SchoolClass.query.filter_by(name="SSLC").first()
        class_id = sslc.id
    body = client.get(f"/admin/students?class_id={class_id}").get_data(as_text=True)
    assert 'name="division_id"' in body


def test_office_division_dropdown_excludes_other_branch(client, app, data):
    # office1 runs High School; the +2 division belongs to Higher Secondary and
    # must not be reachable in their class dropdown at all.
    from app.models import SchoolClass
    login(client, "office1", "Office@123")
    with app.app_context():
        plus2_id = SchoolClass.query.filter_by(name="+2").first().id
    # Trying to filter by the other branch's class returns nothing of theirs.
    body = client.get(f"/admin/students?class_id={plus2_id}").get_data(as_text=True)
    assert "Student C" not in body
