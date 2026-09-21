"""Public video-course site: register, subscribe, watch.

Its own session key, kept apart from staff logins and the parent portal, so a
paying customer can never be mistaken for either. Subscribers are customers -
they have no student record, no class and no attendance.
"""
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    flash,
    request,
    session,
    abort,
)

from ..extensions import db
from ..models import Subscriber, Course, Subscription, Settings
from ..utils.payment import build_upi_link
from ..utils.whatsapp import normalize_phone

learn_bp = Blueprint("learn", __name__, url_prefix="/learn")

SESSION_KEY = "learn_subscriber_id"

MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = timedelta(minutes=15)
_failed_attempts = {}


def current_subscriber():
    subscriber_id = session.get(SESSION_KEY)
    if not subscriber_id:
        return None
    subscriber = db.session.get(Subscriber, subscriber_id)
    if subscriber is None or not subscriber.active:
        return None
    return subscriber


def subscriber_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_subscriber() is None:
            session.pop(SESSION_KEY, None)
            flash("Please sign in to continue.", "info")
            return redirect(url_for("learn.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def _seconds_locked_out(mobile):
    key = (request.remote_addr or "?", normalize_phone(mobile))
    cutoff = datetime.utcnow() - ATTEMPT_WINDOW
    recent = [t for t in _failed_attempts.get(key, []) if t > cutoff]
    if recent:
        _failed_attempts[key] = recent
    else:
        _failed_attempts.pop(key, None)
    if len(recent) < MAX_ATTEMPTS:
        return 0
    return int((recent[-1] + ATTEMPT_WINDOW - datetime.utcnow()).total_seconds())


@learn_bp.app_context_processor
def _inject():
    return {"subscriber": current_subscriber()}


# ------------------------------------------------------------------ catalogue
@learn_bp.route("/")
def catalogue():
    courses = (
        Course.query.filter_by(published=True).order_by(Course.title).all()
    )
    return render_template("learn/catalogue.html", courses=courses)


@learn_bp.route("/course/<int:course_id>")
def course_detail(course_id):
    course = Course.query.get_or_404(course_id)
    if not course.published:
        abort(404)

    me = current_subscriber()
    subscription = me.subscription_for(course.id) if me else None
    return render_template(
        "learn/course_detail.html",
        course=course,
        subscription=subscription,
        can_watch=bool(me and me.can_watch(course.id)),
    )


@learn_bp.route("/course/<int:course_id>/watch/<int:video_id>")
@subscriber_required
def watch(course_id, video_id):
    course = Course.query.get_or_404(course_id)
    me = current_subscriber()

    # Checked on every request, not just hidden in the page: an expired
    # subscriber typing the URL must not reach the lecture.
    if not me.can_watch(course.id):
        flash("Your subscription for this course is not active.", "warning")
        return redirect(url_for("learn.course_detail", course_id=course.id))

    video = next((v for v in course.videos if v.id == video_id and v.published), None)
    if video is None:
        abort(404)

    return render_template(
        "learn/watch.html",
        course=course,
        video=video,
        subscription=me.subscription_for(course.id),
    )


# ------------------------------------------------------------------- accounts
@learn_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_subscriber():
        return redirect(url_for("learn.my_courses"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        mobile = normalize_phone(request.form.get("mobile", ""))
        password = request.form.get("password", "")
        email = request.form.get("email", "").strip()

        if not name or len(mobile) < 10:
            flash("Enter your name and a valid mobile number.", "danger")
        elif len(password) < 6:
            flash("Choose a password of at least 6 characters.", "danger")
        elif Subscriber.query.filter_by(mobile=mobile).first():
            flash("An account already exists for that mobile number. Please sign in.", "warning")
            return redirect(url_for("learn.login"))
        else:
            subscriber = Subscriber(name=name, mobile=mobile, email=email)
            subscriber.set_password(password)
            db.session.add(subscriber)
            db.session.commit()
            session[SESSION_KEY] = subscriber.id
            session.permanent = True
            flash(f"Welcome, {name}.", "success")
            return redirect(url_for("learn.catalogue"))

    return render_template("learn/register.html")


@learn_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        mobile = normalize_phone(request.form.get("mobile", ""))
        password = request.form.get("password", "")

        locked_for = _seconds_locked_out(mobile)
        if locked_for > 0:
            flash(
                f"Too many failed attempts. Please wait {max(locked_for // 60, 1)} minute(s).",
                "danger",
            )
            return render_template("learn/login.html")

        subscriber = Subscriber.query.filter_by(mobile=mobile).first()
        if subscriber and subscriber.active and subscriber.check_password(password):
            _failed_attempts.pop((request.remote_addr or "?", mobile), None)
            session[SESSION_KEY] = subscriber.id
            session.permanent = True
            target = request.args.get("next", "")
            if target.startswith("/learn"):
                return redirect(target)
            return redirect(url_for("learn.my_courses"))

        _failed_attempts.setdefault((request.remote_addr or "?", mobile), []).append(
            datetime.utcnow()
        )
        flash("Mobile number or password is incorrect.", "danger")

    return render_template("learn/login.html")


@learn_bp.route("/logout")
def logout():
    session.pop(SESSION_KEY, None)
    flash("You have been signed out.", "info")
    return redirect(url_for("learn.catalogue"))


@learn_bp.route("/my-courses")
@subscriber_required
def my_courses():
    me = current_subscriber()
    rows = sorted(me.subscriptions, key=lambda s: s.requested_at, reverse=True)
    return render_template("learn/my_courses.html", rows=rows)


# --------------------------------------------------------------- subscribing
@learn_bp.route("/course/<int:course_id>/subscribe", methods=["POST"])
@subscriber_required
def subscribe(course_id):
    course = Course.query.get_or_404(course_id)
    if not course.published:
        abort(404)

    me = current_subscriber()
    existing = me.subscription_for(course.id)
    if existing and existing.status == "pending":
        return redirect(url_for("learn.payment", subscription_id=existing.id))
    if existing and existing.is_current:
        flash("You already have access to this course.", "info")
        return redirect(url_for("learn.course_detail", course_id=course.id))

    subscription = Subscription(
        subscriber_id=me.id, course_id=course.id, amount=course.price
    )
    db.session.add(subscription)
    db.session.commit()
    return redirect(url_for("learn.payment", subscription_id=subscription.id))


@learn_bp.route("/subscription/<int:subscription_id>/payment", methods=["GET", "POST"])
@subscriber_required
def payment(subscription_id):
    subscription = Subscription.query.get_or_404(subscription_id)
    me = current_subscriber()
    if subscription.subscriber_id != me.id:
        abort(403)

    settings = Settings.get()

    if request.method == "POST":
        subscription.payment_note = request.form.get("payment_note", "").strip()[:160]
        db.session.commit()
        flash(
            "Thank you. The academy office will confirm your payment and open the course - "
            "you will see it under My Courses.",
            "success",
        )
        return redirect(url_for("learn.my_courses"))

    upi_link = None
    if settings.upi_id and subscription.amount > 0:
        upi_link = build_upi_link(
            settings.upi_id,
            settings.upi_payee_name or settings.academy_name,
            subscription.amount,
            f"{subscription.course.title} subscription",
        )

    return render_template(
        "learn/payment.html",
        subscription=subscription,
        settings=settings,
        upi_link=upi_link,
    )
