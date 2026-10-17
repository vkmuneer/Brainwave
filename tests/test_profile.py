"""Users maintaining their own name, contact details and photo."""
import io
import struct
import zlib

from conftest import login


def _png():
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + bytes([33, 99, 150] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_profile_page_opens(client, data):
    login(client, "admin", "admin123")
    assert client.get("/account/profile").status_code == 200


def test_user_edits_details_and_uploads_a_photo(client, app, data):
    from app.models import User

    login(client, "admin", "admin123")
    client.post(
        "/account/profile",
        data={"name": "The Principal", "phone": "+91 99999 00000",
              "email": "head@brainwave.in", "photo": (io.BytesIO(_png()), "me.png")},
        content_type="multipart/form-data",
    )
    with app.app_context():
        u = User.query.filter_by(username="admin").first()
        assert u.name == "The Principal"
        assert u.phone == "+91 99999 00000"
        assert u.email == "head@brainwave.in"
        assert u.has_photo

    assert client.get("/photo/staff/1").status_code == 200


def test_a_blank_name_is_rejected(client, app, data):
    from app.models import User

    login(client, "admin", "admin123")
    client.post("/account/profile", data={"name": "   "})
    with app.app_context():
        assert User.query.filter_by(username="admin").first().name == "Administrator"


def test_removing_the_photo(client, app, data):
    from app.models import User

    login(client, "admin", "admin123")
    client.post("/account/profile",
                data={"name": "Admin", "photo": (io.BytesIO(_png()), "me.png")},
                content_type="multipart/form-data")
    client.post("/account/profile", data={"name": "Admin", "remove_photo": "1"})
    with app.app_context():
        assert User.query.filter_by(username="admin").first().photo_data is None
    assert client.get("/photo/staff/1").status_code == 404


def test_an_svg_photo_is_refused(client, app, data):
    from app.models import User

    login(client, "admin", "admin123")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    client.post("/account/profile",
                data={"name": "Admin", "photo": (io.BytesIO(svg), "x.svg")},
                content_type="multipart/form-data")
    with app.app_context():
        assert User.query.filter_by(username="admin").first().photo_data is None


def test_a_teachers_public_record_follows_their_profile(client, app, data):
    """Editing a teacher's own profile updates their public faculty record too."""
    from app.models import User

    login(client, "teach1", "Teach@123")
    client.post("/account/profile",
                data={"name": "Dr Renamed", "phone": "+91 80000 00000"})
    with app.app_context():
        u = User.query.filter_by(username="teach1").first()
        assert u.name == "Dr Renamed"
        assert u.teacher.name == "Dr Renamed"
        assert u.teacher.phone == "+91 80000 00000"
