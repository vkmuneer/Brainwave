from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from ..extensions import db
from ..models import User, PasswordResetRequest, Settings

auth_bp = Blueprint("auth", __name__)

MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = timedelta(minutes=15)

# Recent failed logins, keyed by (client address, username). In-process only,
# so it resets on restart and is per-worker - enough to stop password guessing
# at human or script speed, which is what an internet-facing login page faces.
_failed_attempts = {}


def _attempt_key(username):
    return (request.remote_addr or "?", username.lower())


def _seconds_locked_out(username):
    """Remaining lockout in seconds, or 0 if this client may try again."""
    key = _attempt_key(username)
    cutoff = datetime.utcnow() - ATTEMPT_WINDOW
    recent = [t for t in _failed_attempts.get(key, []) if t > cutoff]

    if recent:
        _failed_attempts[key] = recent
    else:
        _failed_attempts.pop(key, None)

    if len(recent) < MAX_ATTEMPTS:
        return 0
    return int((recent[-1] + ATTEMPT_WINDOW - datetime.utcnow()).total_seconds())


def _portal_enabled():
    settings = Settings.query.get(1)
    return bool(settings and settings.student_login_enabled)


def _is_safe_next(target):
    """Only allow redirects back into this site, so a crafted ?next= cannot
    bounce someone to another domain straight after they log in."""
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        locked_for = _seconds_locked_out(username)
        if locked_for > 0:
            flash(
                f"Too many failed attempts. Please wait {max(locked_for // 60, 1)} "
                "minute(s) and try again.",
                "danger",
            )
            return render_template("login.html", portal_enabled=_portal_enabled())

        user = User.query.filter_by(username=username).first()

        if user and user.active and user.check_password(password):
            _failed_attempts.pop(_attempt_key(username), None)
            login_user(user, remember=True)
            flash(f"Welcome back, {user.name}!", "success")
            next_page = request.args.get("next")
            if _is_safe_next(next_page):
                return redirect(next_page)
            return redirect(url_for("index"))

        _failed_attempts.setdefault(_attempt_key(username), []).append(datetime.utcnow())
        flash("Invalid username or password.", "danger")

    return render_template("login.html", portal_enabled=_portal_enabled())


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        note = request.form.get("note", "").strip()[:255]
        user = User.query.filter_by(username=username).first()

        if user is not None:
            already_waiting = PasswordResetRequest.query.filter_by(
                user_id=user.id, status="pending"
            ).first()
            if already_waiting is None:
                db.session.add(PasswordResetRequest(user_id=user.id, note=note))
                db.session.commit()

        # Deliberately the same response whether or not the account exists:
        # otherwise this page would confirm which usernames are valid to anyone
        # who asked it.
        flash(
            "Thanks - if that account exists, the academy office has been notified "
            "and will set a new password for you.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "danger")
        elif len(new_pw) < 4:
            flash("New password must be at least 4 characters.", "danger")
        elif new_pw != confirm_pw:
            flash("New password and confirmation do not match.", "danger")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("index"))

    return render_template("change_password.html")
