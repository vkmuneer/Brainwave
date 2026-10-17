from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from config import MIN_PASSWORD_LENGTH
from ..extensions import db
from ..models import User, PasswordResetRequest, Settings
from ..utils.images import read_image_upload
from ..utils.email import send_email, mail_configured
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

auth_bp = Blueprint("auth", __name__)

RESET_SALT = "password-reset"
RESET_MAX_AGE = 3600  # one hour


def _reset_serializer():
    from flask import current_app
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _make_reset_token(user):
    return _reset_serializer().dumps(user.id, salt=RESET_SALT)


def _read_reset_token(token):
    """The user id in a valid, unexpired token, or None."""
    try:
        user_id = _reset_serializer().loads(token, salt=RESET_SALT, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return user_id

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
            # Preferred path: email the user a self-service reset link. Falls
            # back to notifying the office when the account has no email on
            # file or mail sending is not configured.
            if user.email and mail_configured():
                link = url_for("auth.reset_password",
                               token=_make_reset_token(user), _external=True)
                send_email(
                    user.email,
                    f"Password reset - {Settings.get().academy_name}",
                    "We received a request to reset your password.\n\n"
                    f"Open this link to choose a new password (valid for one hour):\n{link}\n\n"
                    "If you did not ask for this, you can ignore this email - "
                    "your password will not change.",
                )
            else:
                already_waiting = PasswordResetRequest.query.filter_by(
                    user_id=user.id, status="pending"
                ).first()
                if already_waiting is None:
                    db.session.add(PasswordResetRequest(user_id=user.id, note=note))
                    db.session.commit()

        # Deliberately the same response whether or not the account exists, and
        # whichever path ran: otherwise this page would confirm which usernames
        # (or which have email on file) are valid to anyone who asked it.
        flash(
            "Thanks - if that account exists, we have sent a reset link to the "
            "email on file, or notified the office to set a new password for you.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user_id = _read_reset_token(token)
    user = User.query.get(user_id) if user_id else None
    if user is None:
        flash("That reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        if len(new_pw) < MIN_PASSWORD_LENGTH:
            flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
        elif new_pw != confirm_pw:
            flash("The two passwords do not match.", "danger")
        else:
            user.set_password(new_pw)
            db.session.commit()
            flash("Your password has been reset. You can now sign in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("reset_password.html", token=token)



@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account/profile", methods=["GET", "POST"])
@login_required
def profile():
    """A user maintains their own name, contact details and photo here."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name cannot be blank.", "danger")
            return redirect(url_for("auth.profile"))
        current_user.name = name[:120]
        current_user.phone = request.form.get("phone", "").strip()[:20]
        current_user.email = request.form.get("email", "").strip()[:120]

        if request.form.get("remove_photo"):
            current_user.photo_data = None
            current_user.photo_mimetype = None
        else:
            data, mimetype, error = read_image_upload(request.files.get("photo"))
            if error:
                flash(error, "danger")
                return redirect(url_for("auth.profile"))
            if data is not None:
                current_user.photo_data = data
                current_user.photo_mimetype = mimetype

        # A teacher's public-facing record mirrors the account name and phone,
        # so the faculty section and their own profile never disagree.
        if current_user.role == "teacher" and current_user.teacher is not None:
            current_user.teacher.name = current_user.name
            current_user.teacher.phone = current_user.phone

        db.session.commit()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("auth.profile"))

    return render_template("account_profile.html")


@auth_bp.route("/account/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "danger")
        elif len(new_pw) < MIN_PASSWORD_LENGTH:
            flash(
                f"New password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger"
            )
        elif new_pw != confirm_pw:
            flash("New password and confirmation do not match.", "danger")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("index"))

    return render_template("change_password.html")
