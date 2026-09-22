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


# ------------------------------------------------------- questions & answers
def _require_active_subscription(course_id):
    """The subscriber, if their subscription to this course is current.

    Asking a question or submitting work is part of the course, so it needs the
    same live subscription that watching does.
    """
    me = current_subscriber()
    if me is None or not me.can_watch(course_id):
        return None
    return me


@learn_bp.route("/course/<int:course_id>/questions", methods=["GET", "POST"])
@subscriber_required
def questions(course_id):
    from ..models import CourseQuestion

    course = Course.query.get_or_404(course_id)
    me = _require_active_subscription(course.id)
    if me is None:
        flash("Your subscription for this course is not active.", "warning")
        return redirect(url_for("learn.course_detail", course_id=course.id))

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        video_id = request.form.get("video_id", type=int)
        if not body:
            flash("Please type your question.", "danger")
        elif len(body) > 2000:
            flash("Please keep the question under 2000 characters.", "danger")
        else:
            waiting = CourseQuestion.query.filter_by(
                course_id=course.id, subscriber_id=me.id, answer=None
            ).count()
            if waiting >= 5:
                flash(
                    "You already have several questions waiting for an answer. "
                    "Please wait for those to be answered first.",
                    "warning",
                )
            else:
                db.session.add(
                    CourseQuestion(
                        course_id=course.id,
                        subscriber_id=me.id,
                        video_id=video_id or None,
                        body=body,
                    )
                )
                db.session.commit()
                flash("Your question has been sent to the academy.", "success")
        return redirect(url_for("learn.questions", course_id=course.id))

    rows = (
        CourseQuestion.query.filter_by(course_id=course.id)
        .order_by(CourseQuestion.created_at.desc())
        .limit(200)
        .all()
    )
    # Answered questions help everyone on the course; an unanswered one is
    # nobody's business but the asker's.
    visible = [q for q in rows if q.is_answered or q.subscriber_id == me.id]

    return render_template(
        "learn/questions.html", course=course, questions=visible, me=me
    )


# ------------------------------------------------------------- assignments
@learn_bp.route("/course/<int:course_id>/assignments")
@subscriber_required
def assignments(course_id):
    from ..models import Assignment

    course = Course.query.get_or_404(course_id)
    me = _require_active_subscription(course.id)
    if me is None:
        flash("Your subscription for this course is not active.", "warning")
        return redirect(url_for("learn.course_detail", course_id=course.id))

    rows = (
        Assignment.query.filter_by(course_id=course.id, published=True)
        .order_by(Assignment.created_at.desc())
        .all()
    )
    return render_template(
        "learn/assignments.html",
        course=course,
        assignments=rows,
        me=me,
    )


@learn_bp.route("/assignment/<int:assignment_id>", methods=["GET", "POST"])
@subscriber_required
def assignment_detail(assignment_id):
    from ..models import Assignment, AssignmentSubmission
    from ..utils.images import read_image_upload

    assignment = Assignment.query.get_or_404(assignment_id)
    if not assignment.published:
        abort(404)

    me = _require_active_subscription(assignment.course_id)
    if me is None:
        flash("Your subscription for this course is not active.", "warning")
        return redirect(url_for("learn.course_detail", course_id=assignment.course_id))

    submission = assignment.submission_for(me.id)

    if request.method == "POST":
        if submission and submission.is_evaluated:
            # Letting work change after marking would leave the mark describing
            # something that no longer exists.
            flash("This has already been marked and cannot be changed.", "warning")
            return redirect(url_for("learn.assignment_detail", assignment_id=assignment.id))

        text = request.form.get("answer_text", "").strip()
        link = request.form.get("answer_link", "").strip()
        data, mimetype, error = read_image_upload(request.files.get("attachment"))

        if error:
            flash(error, "danger")
            return redirect(url_for("learn.assignment_detail", assignment_id=assignment.id))
        if link and not link.lower().startswith(("http://", "https://")):
            flash("A link must start with http:// or https://", "danger")
            return redirect(url_for("learn.assignment_detail", assignment_id=assignment.id))
        if not text and not link and not data:
            flash("Write an answer, attach a picture, or give a link.", "danger")
            return redirect(url_for("learn.assignment_detail", assignment_id=assignment.id))

        if submission is None:
            submission = AssignmentSubmission(
                assignment_id=assignment.id, subscriber_id=me.id
            )
            db.session.add(submission)

        submission.answer_text = text[:5000]
        submission.answer_link = link[:500]
        if data:
            submission.attachment_data = data
            submission.attachment_mimetype = mimetype
        submission.submitted_at = datetime.utcnow()
        db.session.commit()
        flash("Your work has been submitted.", "success")
        return redirect(url_for("learn.assignment_detail", assignment_id=assignment.id))

    return render_template(
        "learn/assignment_detail.html",
        assignment=assignment,
        submission=submission,
        me=me,
    )


@learn_bp.route("/submission/<int:submission_id>/attachment")
@subscriber_required
def submission_attachment(submission_id):
    from ..models import AssignmentSubmission
    from io import BytesIO
    from flask import send_file

    submission = AssignmentSubmission.query.get_or_404(submission_id)
    me = current_subscriber()
    if submission.subscriber_id != me.id:
        abort(403)
    if not submission.attachment_data:
        abort(404)
    return send_file(
        BytesIO(submission.attachment_data),
        mimetype=submission.attachment_mimetype or "image/png",
    )
