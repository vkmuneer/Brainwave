"""Home-page intro video: admin uploads an MP4, the home page shows it."""
import io, struct, zlib
import pytest
from conftest import login


@pytest.fixture(autouse=True)
def _tmp_media(app, tmp_path):
    app.config["MEDIA_DIR"] = str(tmp_path / "media")


def _mp4():
    # Minimal bytes that pass the 'ftyp' magic-byte check.
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _png():
    def chunk(k, p): b = k + p; return struct.pack(">I", len(p)) + b + struct.pack(">I", zlib.crc32(b))
    raw = b"".join(b"\x00" + bytes([10, 20, 30] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_admin_uploads_video_shown_on_home(client, app, data):
    from app.models import Settings
    login(client, "admin", "admin123")
    client.post("/admin/home-video",
                data={"title": "Inside Brainwave", "text": "A quick tour",
                      "video": (io.BytesIO(_mp4()), "intro.mp4"),
                      "poster": (io.BytesIO(_png()), "poster.png")},
                content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        st = Settings.get()
        assert st.has_home_video
        assert st.home_video_mimetype == "video/mp4"
        assert st.home_video_poster is not None

    assert client.get("/home-video/file").status_code == 200
    assert client.get("/home-video/poster").status_code == 200
    home = app.test_client().get("/home").get_data(as_text=True)
    assert 'id="tour"' in home and "Inside Brainwave" in home


def test_no_video_no_section(client, app, data):
    home = client.get("/home").get_data(as_text=True)
    assert 'id="tour"' not in home
    assert client.get("/home-video/file").status_code == 404


def test_an_image_is_refused_as_video(client, app, data):
    from app.models import Settings
    login(client, "admin", "admin123")
    client.post("/admin/home-video",
                data={"video": (io.BytesIO(_png()), "not.mp4")},
                content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        assert Settings.get().has_home_video is False


def test_removing_the_video(client, app, data):
    from app.models import Settings
    login(client, "admin", "admin123")
    client.post("/admin/home-video",
                data={"video": (io.BytesIO(_mp4()), "intro.mp4")},
                content_type="multipart/form-data", follow_redirects=True)
    client.post("/admin/home-video", data={"remove_video": "1"}, follow_redirects=True)
    with app.app_context():
        assert Settings.get().has_home_video is False
    assert client.get("/home-video/file").status_code == 404


def test_office_cannot_manage_video(client, app, data):
    login(client, "office1", "Office@123")
    r = client.get("/admin/home-video")
    assert r.status_code == 403
