import os

from io import BytesIO

from flask import Blueprint, render_template, abort, send_file, current_app

from ..models import Student, Settings
from ..utils.payment import verify_pay_token, build_upi_link

public_bp = Blueprint("public", __name__)


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
