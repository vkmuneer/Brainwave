import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


def _database_uri():
    # Render/Heroku-style hosts sometimes inject an empty DATABASE_URL env var
    # (rather than omitting it) and use the legacy "postgres://" scheme, which
    # SQLAlchemy 1.4+ rejects, so normalize both cases here.
    uri = os.environ.get("DATABASE_URL") or f"sqlite:///{os.path.join(basedir, 'brainwave.db')}"
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return uri


# Short passwords are the weakest point of an internet-facing login.
MIN_PASSWORD_LENGTH = 8

# Values shipped in .env.example / used as the local default. Session cookies
# are signed with SECRET_KEY, so anyone who knows it can forge a login as any
# user - and these are published in the repo.
PLACEHOLDER_SECRET_KEYS = {
    "dev-secret-key-change-me",
    "change-this-to-a-long-random-string",
}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)

    # Session cookies. HttpOnly keeps them away from any script on the page;
    # SameSite=Lax stops another site posting as a signed-in user.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # Set SECURE_COOKIES=1 once the site is on https, so the session cookie is
    # never sent over a plain connection. Left off locally, where http is all
    # there is - switching it on without https silently breaks every login.
    SESSION_COOKIE_SECURE = os.environ.get("SECURE_COOKIES", "0") == "1"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    # Caps an upload before it is read into memory. Most uploads (a logo, a
    # photo, a spreadsheet) are tiny; the one large one is a short home-page
    # intro video, so the ceiling is sized for that.
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024
    # A home-page video is held to this; images keep their own 2 MB check.
    MAX_VIDEO_BYTES = 50 * 1024 * 1024

    # Set BEHIND_PROXY=1 on cPanel/nginx so the client's real address is used
    # for login rate limiting rather than the proxy's.
    BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "0") == "1"

    DEFAULT_ADMIN_USERNAME = os.environ.get("DEFAULT_ADMIN_USERNAME") or "admin"
    DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD") or "admin123"

    # Email (for password-reset links). Defaults suit Gmail; set MAIL_USERNAME
    # and MAIL_PASSWORD (a 16-character Google "app password", not the account
    # password) to switch sending on. Left blank, reset falls back to the
    # office-approval flow.
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "1") == "1"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "") or os.environ.get("MAIL_USERNAME", "")

    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

    DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "91")

    # Base tuition fees per class, as requested for Brainwave Academy.
    DEFAULT_CLASS_FEES = {
        "9": 10000,
        "SSLC": 12500,
        "+1": 20000,
        "+2": 20000,
    }
