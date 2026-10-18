"""Home-page banner slider: admin manages it, the home page shows active ones."""
import io, struct, zlib
from conftest import login


def _png():
    def chunk(k, p): b = k + p; return struct.pack(">I", len(p)) + b + struct.pack(">I", zlib.crc32(b))
    raw = b"".join(b"\x00" + bytes([20, 60, 120] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _add(client, caption="Admissions open", link=""):
    return client.post("/admin/banners",
                       data={"caption": caption, "link_url": link, "image": (io.BytesIO(_png()), "b.png")},
                       content_type="multipart/form-data", follow_redirects=True)


def test_admin_adds_a_banner_and_it_shows_on_home(client, app, data):
    from app.models import BannerImage
    login(client, "admin", "admin123")
    _add(client, "Admissions open 2026")
    with app.app_context():
        b = BannerImage.query.first()
        assert b is not None and b.caption == "Admissions open 2026"
        bid = b.id
    assert client.get(f"/banner/image/{bid}").status_code == 200
    home = app.test_client().get("/home").get_data(as_text=True)
    assert 'id="bwBanner"' in home
    assert "Admissions open 2026" in home


def test_hidden_banner_not_on_home(client, app, data):
    from app.models import BannerImage
    login(client, "admin", "admin123")
    _add(client, "Draft banner")
    with app.app_context():
        bid = BannerImage.query.first().id
    client.post(f"/admin/banners/{bid}/toggle", follow_redirects=True)
    home = app.test_client().get("/home").get_data(as_text=True)
    assert "Draft banner" not in home


def test_no_banners_means_no_carousel(client, app, data):
    home = client.get("/home").get_data(as_text=True)
    assert 'id="bwBanner"' not in home


def test_order_controls_slide_sequence(client, app, data):
    from app.extensions import db
    from app.models import BannerImage
    login(client, "admin", "admin123")
    _add(client, "First added")
    _add(client, "Second added")
    with app.app_context():
        first, second = BannerImage.query.order_by(BannerImage.created_at).all()
        # Put the second one ahead of the first.
        first_id, second_id = first.id, second.id
    client.post(f"/admin/banners/{second_id}/update",
                data={"caption": "Second added", "link_url": "", "sort_order": "1"},
                follow_redirects=True)
    home = app.test_client().get("/home").get_data(as_text=True)
    assert home.index("Second added") < home.index("First added")


def test_delete_removes_banner(client, app, data):
    from app.models import BannerImage
    login(client, "admin", "admin123")
    _add(client)
    with app.app_context():
        bid = BannerImage.query.first().id
    client.post(f"/admin/banners/{bid}/delete", follow_redirects=True)
    with app.app_context():
        assert BannerImage.query.count() == 0


def test_office_cannot_manage_banners(client, app, data):
    login(client, "office1", "Office@123")
    r = client.post("/admin/banners",
                    data={"caption": "x", "image": (io.BytesIO(_png()), "b.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 403


def test_svg_banner_refused(client, app, data):
    from app.models import BannerImage
    login(client, "admin", "admin123")
    client.post("/admin/banners",
                data={"caption": "x", "image": (io.BytesIO(b"<svg></svg>"), "x.svg")},
                content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        assert BannerImage.query.count() == 0
