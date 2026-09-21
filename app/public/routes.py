import os

from io import BytesIO

from flask import Blueprint, render_template, abort, send_file, current_app

from ..models import Student, Settings
from ..utils.payment import verify_pay_token, build_upi_link

public_bp = Blueprint("public", __name__)


@public_bp.route("/home")
def home():
    """The academy's public front page.

    Built from real records - the classes taught, their divisions, published
    video courses and the contact details under Settings - so it stays true as
    the academy changes rather than being a separate copy to maintain.
    """
    from ..models import SchoolClass, Subject, Teacher, Course, CLASS_ORDER

    settings = Settings.get()
    classes = sorted(
        SchoolClass.query.all(),
        key=lambda c: CLASS_ORDER.index(c.name) if c.name in CLASS_ORDER else 99,
    )

    stats = [
        {"value": len(classes), "label": "Classes coached"},
        {"value": sum(len(c.divisions) for c in classes), "label": "Divisions"},
        {"value": Subject.query.count(), "label": "Subjects taught"},
        {"value": Teacher.query.filter_by(active=True).count(), "label": "Teachers"},
    ]

    highlights = [
        {"icon": "bi-check2-square", "title": "Daily attendance", "text": "Parents told the same day"},
        {"icon": "bi-graph-up-arrow", "title": "Exam analysis", "text": "Subject by subject"},
        {"icon": "bi-cash-coin", "title": "Fees online", "text": "Pay by UPI, receipts kept"},
        {"icon": "bi-play-btn", "title": "Class videos", "text": "Rewatch any lesson"},
    ]

    features = [
        {
            "icon": "bi-people",
            "title": "Small divisions",
            "text": "Classes are split into divisions so a teacher knows every student, "
                    "and nobody sits at the back unnoticed.",
        },
        {
            "icon": "bi-clipboard-check",
            "title": "Attendance you hear about",
            "text": "Registers are taken every session and absences reach parents by "
                    "WhatsApp the same day, not at the end of the month.",
        },
        {
            "icon": "bi-bar-chart-line",
            "title": "Honest exam reporting",
            "text": "Every exam produces subject-wise analysis, a rank and a report card - "
                    "with the weak subjects named rather than averaged away.",
        },
        {
            "icon": "bi-wallet2",
            "title": "Fees in installments",
            "text": "Pay across the year on a published schedule, with a reminder each "
                    "month showing exactly what is due and what has been received.",
        },
        {
            "icon": "bi-phone",
            "title": "Everything on your phone",
            "text": "Parents sign in to see attendance, marks, fee balance and class "
                    "videos. No app to install.",
        },
        {
            "icon": "bi-chat-dots",
            "title": "Talk to the office",
            "text": "Ask a question or raise a concern from the portal and get a written "
                    "reply you can look back at.",
        },
    ]

    return render_template(
        "public/home.html",
        settings=settings,
        classes=classes,
        stats=stats,
        highlights=highlights,
        features=features,
        courses=Course.query.filter_by(published=True).order_by(Course.title).limit(6).all(),
        portal_open=settings.student_login_enabled,
    )


@public_bp.route("/logo")
def logo():
    """Serves the uploaded academy logo, falling back to the bundled one.

    Unauthenticated because the login page shows it. no-cache so a freshly
    uploaded logo appears immediately rather than after a browser cache expiry.
    """
    settings = Settings.query.get(1)
    if settings is not None and settings.logo_data:
        response = send_file(
            BytesIO(settings.logo_data),
            mimetype=settings.logo_mimetype or "image/png",
        )
    else:
        path = os.path.join(current_app.static_folder, "img", "logo.svg")
        response = send_file(path, mimetype="image/svg+xml")

    response.headers["Cache-Control"] = "no-cache"
    return response


@public_bp.route("/pay/<token>")
def pay(token):
    student_id = verify_pay_token(token)
    if student_id is None:
        abort(404)

    student = Student.query.get_or_404(student_id)
    settings = Settings.get()
    pending = student.pending_fee

    upi_link = None
    if pending > 0 and settings.upi_id:
        note = f"Brainwave Academy fee - {student.name} ({student.admission_no})"
        upi_link = build_upi_link(settings.upi_id, settings.upi_payee_name, pending, note)

    return render_template(
        "public/pay.html",
        student=student,
        settings=settings,
        pending=pending,
        upi_link=upi_link,
    )
