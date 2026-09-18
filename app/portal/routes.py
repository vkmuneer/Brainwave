"""Student/parent portal.

Parents sign in with the WhatsApp number the academy holds for them, used as
both username and password. That is a deliberately low bar - a phone number is
not a secret - so this portal is read-only, shows a family only its own
children, and is disabled until an admin turns it on under Settings.

It keeps its own session key rather than Flask-Login, so a signed-in parent is
never mistaken for a staff account by the admin/teacher views.
"""
from datetime import datetime, timedelta
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
    send_file,
)

from ..models import Student, Settings, Attendance, VideoClass, Exam
from ..utils.payment import build_pay_url
from ..utils.whatsapp import normalize_phone
from ..utils.exam_analysis import compute_exam_results
from ..utils.pdf import render_pdf

portal_bp = Blueprint("portal", __name__, url_prefix="/portal")

SESSION_KEY = "portal_phone"

MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = timedelta(minutes=15)
_failed_attempts = {}


def portal_enabled():
    settings = Settings.query.get(1)
    return bool(settings and settings.student_login_enabled)


def _students_for_phone(phone):
    """Every active student whose parent WhatsApp number matches.

    Compared after normalising both sides, so a parent can type the number with
    or without the country code. Siblings share a number, so this is a list.
    """
    wanted = normalize_phone(phone)
    if not wanted or len(wanted) < 10:
        return []
    return [
        s
        for s in Student.query.filter_by(active=True).all()
        if normalize_phone(s.parent_whatsapp) == wanted
    ]


def current_students():
    phone = session.get(SESSION_KEY)
    return _students_for_phone(phone) if phone else []


def portal_login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not portal_enabled():
            session.pop(SESSION_KEY, None)
            return render_template("portal/disabled.html"), 403
        if not session.get(SESSION_KEY):
            return redirect(url_for("portal.login"))
        if not current_students():
            # Their record was removed or deactivated while signed in.
            session.pop(SESSION_KEY, None)
            return redirect(url_for("portal.login"))
        return view(*args, **kwargs)

    return wrapper


def _selected_student():
    """The child being viewed - the first, or ?student_id= when a family has
    more than one and it belongs to this phone number."""
    students = current_students()
    student_id = request.args.get("student_id", type=int)
    if student_id:
        for student in students:
            if student.id == student_id:
                return student
        abort(403)
    return students[0]


def _seconds_locked_out(phone):
    key = (request.remote_addr or "?", normalize_phone(phone))
    cutoff = datetime.utcnow() - ATTEMPT_WINDOW
    recent = [t for t in _failed_attempts.get(key, []) if t > cutoff]
    if recent:
        _failed_attempts[key] = recent
    else:
        _failed_attempts.pop(key, None)
    if len(recent) < MAX_ATTEMPTS:
        return 0
    return int((recent[-1] + ATTEMPT_WINDOW - datetime.utcnow()).total_seconds())


@portal_bp.route("/login", methods=["GET", "POST"])
def login():
    if not portal_enabled():
        return render_template("portal/disabled.html"), 403

    if session.get(SESSION_KEY) and current_students():
        return redirect(url_for("portal.home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        locked_for = _seconds_locked_out(username)
        if locked_for > 0:
            flash(
                f"Too many failed attempts. Please wait {max(locked_for // 60, 1)} minute(s).",
                "danger",
            )
            return render_template("portal/login.html")

        students = _students_for_phone(username)
        if students and normalize_phone(password) == normalize_phone(username):
            _failed_attempts.pop((request.remote_addr or "?", normalize_phone(username)), None)
            session[SESSION_KEY] = normalize_phone(username)
            session.permanent = True
            return redirect(url_for("portal.home"))

        _failed_attempts.setdefault(
            (request.remote_addr or "?", normalize_phone(username)), []
        ).append(datetime.utcnow())
        flash("We could not find that mobile number. Please contact the academy office.", "danger")

    return render_template("portal/login.html")


@portal_bp.route("/logout")
def logout():
    session.pop(SESSION_KEY, None)
    flash("You have been logged out.", "info")
    return redirect(url_for("portal.login"))


@portal_bp.route("/")
@portal_login_required
def home():
    student = _selected_student()
    return render_template(
        "portal/home.html",
        student=student,
        students=current_students(),
        pay_url=build_pay_url(student.id) if student.pending_fee > 0 else None,
    )


@portal_bp.route("/attendance")
@portal_login_required
def attendance():
    student = _selected_student()
    records = (
        Attendance.query.filter_by(student_id=student.id)
        .order_by(Attendance.date.desc())
        .limit(60)
        .all()
    )
    present = sum(1 for r in records if r.status == "present")
    return render_template(
        "portal/attendance.html",
        student=student,
        students=current_students(),
        records=records,
        present=present,
        total=len(records),
    )


@portal_bp.route("/videos")
@portal_login_required
def videos():
    student = _selected_student()
    available = [
        v
        for v in VideoClass.query.order_by(VideoClass.created_at.desc()).all()
        if v.visible_to(student)
    ]
    return render_template(
        "portal/videos.html",
        student=student,
        students=current_students(),
        videos=available,
    )


def _exam_for_student(exam_id, student):
    """An exam from this student's own class, or 404 - so a guessed exam id
    from another class cannot be opened."""
    exam = Exam.query.get_or_404(exam_id)
    if exam.class_id != student.class_id:
        abort(404)
    return exam


def _student_result(exam, student):
    """This student's row from the full class ranking, so the rank shown is
    their real position rather than one computed in isolation."""
    classmates = Student.query.filter_by(class_id=exam.class_id, active=True).all()
    for result in compute_exam_results(exam, classmates):
        if result["student"].id == student.id:
            return result
    return None


@portal_bp.route("/marks")
@portal_login_required
def marks():
    student = _selected_student()
    exams = (
        Exam.query.filter_by(class_id=student.class_id).order_by(Exam.exam_date.desc()).all()
    )
    rows = []
    for exam in exams:
        result = _student_result(exam, student)
        if result and result["complete"]:
            rows.append({"exam": exam, "result": result})

    return render_template(
        "portal/marks.html",
        student=student,
        students=current_students(),
        rows=rows,
        pending_count=len(exams) - len(rows),
    )


@portal_bp.route("/marks/<int:exam_id>")
@portal_login_required
def marks_detail(exam_id):
    student = _selected_student()
    exam = _exam_for_student(exam_id, student)
    result = _student_result(exam, student)
    if not result or not result["complete"]:
        abort(404)

    return render_template(
        "portal/marks_detail.html",
        student=student,
        students=current_students(),
        exam=exam,
        result=result,
    )


@portal_bp.route("/marks/<int:exam_id>/report-card")
@portal_login_required
def report_card(exam_id):
    student = _selected_student()
    exam = _exam_for_student(exam_id, student)
    result = _student_result(exam, student)
    if not result or not result["complete"]:
        abort(404)

    buffer = render_pdf(
        "pdf/report_card_pdf.html",
        settings=Settings.get(),
        exam=exam,
        student=student,
        result=result,
    )
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"report_card_{student.admission_no}_{exam.name.replace(' ', '_')}.pdf",
    )
