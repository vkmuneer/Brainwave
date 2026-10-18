"""Management / leadership team section on the home page."""
import io, struct, zlib
from conftest import login


def _png():
    def chunk(k, p): b = k + p; return struct.pack(">I", len(p)) + b + struct.pack(">I", zlib.crc32(b))
    raw = b"".join(b"\x00" + bytes([44, 88, 120] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _add(client, name="Dr Rahim", desig="Managing Director", bio="25 years in education."):
    return client.post("/admin/team",
                       data={"name": name, "designation": desig, "credentials": "M.Sc, B.Ed",
                             "bio": bio, "photo": (io.BytesIO(_png()), "p.png")},
                       content_type="multipart/form-data", follow_redirects=True)


def test_admin_adds_member_shown_on_home(client, app, data):
    from app.models import TeamMember
    login(client, "admin", "admin123")
    _add(client, "Dr Rahim", "Chairman", "Founded the academy in 2001.")
    with app.app_context():
        m = TeamMember.query.first()
        assert m is not None and m.designation == "Chairman"
        mid = m.id
    assert client.get(f"/photo/team/{mid}").status_code == 200
    home = app.test_client().get("/home").get_data(as_text=True)
    assert 'id="team"' in home
    assert "Dr Rahim" in home
    assert "Founded the academy in 2001." in home


def test_team_appears_above_faculty(client, app, data):
    from app.extensions import db
    from app.models import TeamMember, Teacher
    login(client, "admin", "admin123")
    _add(client, "Dr Rahim")
    with app.app_context():
        t = Teacher.query.first()
        t.show_on_website = True
        t.designation = "Physics Faculty"
        db.session.commit()
    home = app.test_client().get("/home").get_data(as_text=True)
    assert 'id="team"' in home and 'id="faculty"' in home
    assert home.index('id="team"') < home.index('id="faculty"')


def test_no_members_no_section(client, app, data):
    home = client.get("/home").get_data(as_text=True)
    assert 'id="team"' not in home


def test_update_and_order(client, app, data):
    from app.models import TeamMember
    login(client, "admin", "admin123")
    _add(client, "First One")
    _add(client, "Second One")
    with app.app_context():
        first, second = TeamMember.query.order_by(TeamMember.created_at).all()
        second_id = second.id
    client.post(f"/admin/team/{second_id}/update",
                data={"name": "Second One", "designation": "MD", "credentials": "",
                      "bio": "", "sort_order": "1"}, follow_redirects=True)
    home = app.test_client().get("/home").get_data(as_text=True)
    assert home.index("Second One") < home.index("First One")


def test_hidden_member_not_on_home(client, app, data):
    from app.models import TeamMember
    login(client, "admin", "admin123")
    _add(client, "Hidden Person")
    with app.app_context():
        mid = TeamMember.query.first().id
    client.post(f"/admin/team/{mid}/toggle", follow_redirects=True)
    home = app.test_client().get("/home").get_data(as_text=True)
    assert "Hidden Person" not in home


def test_delete_member(client, app, data):
    from app.models import TeamMember
    login(client, "admin", "admin123")
    _add(client)
    with app.app_context():
        mid = TeamMember.query.first().id
    client.post(f"/admin/team/{mid}/delete", follow_redirects=True)
    with app.app_context():
        assert TeamMember.query.count() == 0


def test_office_cannot_manage_team(client, app, data):
    login(client, "office1", "Office@123")
    r = client.post("/admin/team", data={"name": "X"})
    assert r.status_code == 403


def test_member_without_photo_uses_initial(client, app, data):
    from app.models import TeamMember
    login(client, "admin", "admin123")
    client.post("/admin/team", data={"name": "No Photo", "designation": "Director",
                "credentials": "", "bio": "Bio here."}, follow_redirects=True)
    with app.app_context():
        assert TeamMember.query.first().has_photo is False
    home = app.test_client().get("/home").get_data(as_text=True)
    assert "No Photo" in home
