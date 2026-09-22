"""Thumbnails, and the menu being present wherever a staff member is."""
import io
import struct
import zlib

import pytest

from conftest import login


def _png(width=8, height=8):
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes([40, 90, 120] * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_course_cover_is_stored_and_served(client, app, data):
    from app.models import Course

    login(client, "admin", "admin123")
    client.post(
        "/admin/courses/new",
        data={
            "title": "Pictured Course", "price": 100, "duration_days": 30,
            "published": "1", "thumbnail": (io.BytesIO(_png()), "cover.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with app.app_context():
        course = Course.query.filter_by(title="Pictured Course").first()
        assert course.thumbnail_data is not None
        course_id = course.id

    response = client.get(f"/thumb/course/{course_id}")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/")


def test_course_without_a_cover_returns_not_found(client, app, data):
    from app.extensions import db
    from app.models import Course

    with app.app_context():
        course = Course(title="Plain", price=0, published=True)
        db.session.add(course)
        db.session.commit()
        course_id = course.id

    assert client.get(f"/thumb/course/{course_id}").status_code == 404


def test_an_svg_is_refused_as_a_thumbnail(client, app, data):
    from app.models import Course

    login(client, "admin", "admin123")
    client.post(
        "/admin/courses/new",
        data={
            "title": "SVG Course", "price": 100, "duration_days": 30,
            "thumbnail": (io.BytesIO(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "x.svg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with app.app_context():
        assert Course.query.filter_by(title="SVG Course").first() is None


def test_a_file_pretending_to_be_a_png_is_refused(client, app, data):
    """The browser's content type is not evidence; the bytes are."""
    from app.models import Course

    login(client, "admin", "admin123")
    client.post(
        "/admin/courses/new",
        data={
            "title": "Fake PNG", "price": 100, "duration_days": 30,
            "thumbnail": (io.BytesIO(b"MZ\x90\x00 not really an image"), "x.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with app.app_context():
        assert Course.query.filter_by(title="Fake PNG").first() is None


def test_class_video_thumbnail_round_trip(client, app, data):
    from app.extensions import db
    from app.models import Teacher, VideoClass

    with app.app_context():
        teacher = Teacher.query.first()
        division_id = teacher.divisions[0].id

    login(client, "teach1", "Teach@123")
    client.post(
        "/teacher/videos/add",
        data={
            "title": "With picture", "target": f"div:{division_id}",
            "url": "https://youtu.be/AZWyU5pkjL4",
            "thumbnail": (io.BytesIO(_png()), "t.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with app.app_context():
        video = VideoClass.query.filter_by(title="With picture").first()
        assert video is not None and video.thumbnail_data is not None
        video_id = video.id

    assert client.get(f"/thumb/class-video/{video_id}").status_code == 200


@pytest.mark.parametrize("url", ["/home", "/learn/", "/admin/dashboard"])
def test_signed_in_staff_keep_the_menu_everywhere(client, data, url):
    login(client, "admin", "admin123")
    body = client.get(url, follow_redirects=True).get_data(as_text=True)
    assert 'navbar-nav me-auto' in body, url


@pytest.mark.parametrize("url", ["/home", "/learn/"])
def test_visitors_do_not_see_the_staff_menu(client, data, url):
    body = client.get(url, follow_redirects=True).get_data(as_text=True)
    assert 'navbar-nav me-auto' not in body, url


def test_home_page_numbers_are_set_up_to_animate(client, data):
    body = client.get("/home").get_data(as_text=True)
    assert "data-count-to" in body
    assert "prefers-reduced-motion" in body
