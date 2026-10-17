"""The gallery: office/admin add photos, the public page shows published ones."""
import io
import struct
import zlib

from conftest import login


def _png():
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + bytes([80, 160, 210] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _add(client, caption="Annual day"):
    return client.post("/admin/gallery",
                       data={"caption": caption, "image": (io.BytesIO(_png()), "p.png")},
                       content_type="multipart/form-data", follow_redirects=True)


def test_office_can_add_a_photo_and_it_shows_publicly(client, app, data):
    from app.models import GalleryImage

    login(client, "office1", "Office@123")
    _add(client, "Sports meet")
    with app.app_context():
        img = GalleryImage.query.first()
        assert img is not None
        assert img.caption == "Sports meet"
        assert img.uploaded_by == "Office One"
        image_id = img.id

    assert client.get(f"/gallery/image/{image_id}").status_code == 200
    public = app.test_client().get("/gallery").get_data(as_text=True)
    assert "Sports meet" in public


def test_a_hidden_photo_is_not_on_the_public_page(client, app, data):
    from app.models import GalleryImage

    login(client, "admin", "admin123")
    _add(client, "Secret draft")
    with app.app_context():
        image_id = GalleryImage.query.first().id
    client.post(f"/admin/gallery/{image_id}/toggle", follow_redirects=True)

    public = app.test_client().get("/gallery").get_data(as_text=True)
    assert "Secret draft" not in public


def test_delete_removes_the_photo(client, app, data):
    from app.models import GalleryImage

    login(client, "admin", "admin123")
    _add(client)
    with app.app_context():
        image_id = GalleryImage.query.first().id
    client.post(f"/admin/gallery/{image_id}/delete", follow_redirects=True)
    with app.app_context():
        assert GalleryImage.query.count() == 0


def test_an_svg_is_refused(client, app, data):
    from app.models import GalleryImage

    login(client, "admin", "admin123")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    client.post("/admin/gallery",
                data={"caption": "x", "image": (io.BytesIO(svg), "x.svg")},
                content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        assert GalleryImage.query.count() == 0


def test_a_visitor_cannot_add_photos(client, app, data):
    # Not logged in -> redirected to login, nothing created.
    client.post("/admin/gallery",
                data={"caption": "x", "image": (io.BytesIO(_png()), "p.png")},
                content_type="multipart/form-data")
    from app.models import GalleryImage
    with app.app_context():
        assert GalleryImage.query.count() == 0


def test_a_teacher_cannot_add_photos(client, app, data):
    login(client, "teach1", "Teach@123")
    resp = client.post("/admin/gallery",
                       data={"caption": "x", "image": (io.BytesIO(_png()), "p.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 403
    from app.models import GalleryImage
    with app.app_context():
        assert GalleryImage.query.count() == 0


def test_footer_links_to_the_gallery(client, data):
    body = client.get("/home").get_data(as_text=True)
    assert "/gallery" in body
