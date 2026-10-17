"""Social media links: admin sets them, the footer shows them, junk is refused."""
from conftest import login


def _save(client, **fields):
    data = {"academy_name": "Brainwave Academy"}
    data.update(fields)
    return client.post("/admin/settings", data=data, follow_redirects=True)


def test_admin_saves_links_and_footer_shows_clickable_icons(client, app, data):
    login(client, "admin", "admin123")
    _save(
        client,
        instagram_url="https://instagram.com/brainwave",
        youtube_url="https://youtube.com/@brainwave",
    )

    from app.models import Settings
    with app.app_context():
        s = Settings.get()
        assert s.instagram_url == "https://instagram.com/brainwave"
        assert s.youtube_url == "https://youtube.com/@brainwave"

    body = client.get("/home").get_data(as_text=True)
    assert 'href="https://instagram.com/brainwave"' in body
    assert "bi-instagram" in body
    assert "bi-youtube" in body
    # Facebook was left blank, so its icon is not drawn.
    assert "bi-facebook" not in body


def test_a_bare_domain_becomes_https(client, app, data):
    login(client, "admin", "admin123")
    _save(client, facebook_url="facebook.com/brainwave")
    from app.models import Settings
    with app.app_context():
        assert Settings.get().facebook_url == "https://facebook.com/brainwave"


def test_a_javascript_url_is_refused(client, app, data):
    login(client, "admin", "admin123")
    _save(client, instagram_url="javascript:alert(document.cookie)")
    from app.models import Settings
    with app.app_context():
        assert Settings.get().instagram_url == ""
    body = client.get("/home").get_data(as_text=True)
    assert "javascript:alert" not in body


def test_no_links_means_no_icon_row(client, app, data):
    login(client, "admin", "admin123")
    _save(client)  # all social fields blank
    body = client.get("/home").get_data(as_text=True)
    assert 'class="social-icons"' not in body
