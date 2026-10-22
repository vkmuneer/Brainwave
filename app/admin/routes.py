import calendar
from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.parse import urlparse

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    flash,
    request,
    send_file,
    abort,
    current_app,
)
from flask_login import login_required, current_user
from sqlalchemy import func

from config import MIN_PASSWORD_LENGTH
from ..extensions import db
from ..models import (
    SchoolClass,
    Division,
    User,
    Teacher,
    Student,
    FeePayment,
    Attendance,
    MessageLog,
    Settings,
    Subject,
    Place,
    SchoolMaster,
    Exam,
    ExamSubject,
    ExamMark,
    PasswordResetRequest,
    VideoClass,
    AuditLog,
    Branch,
    Feedback,
    Course,
    CourseVideo,
    Subscription,
    CourseQuestion,
    Assignment,
    AssignmentSubmission,
    GalleryImage,
    BannerImage,
    TeamMember,
    TeacherPayment,
    ExpenseEntry,
    IncomeEntry,
    TimetableSlot,
    AcademicYear,
)
from ..utils.decorators import admin_required, office_required
from ..utils.scope import (
    limit_students,
    visible_classes,
    visible_divisions,
    visible_class_ids,
    ensure_student_visible,
    ensure_division_visible,
    ensure_class_visible,
    current_branch,
    resolved_class_ids,
    scope_payments,
)
from ..utils.attendance import (
    resolve_session,
    session_label,
    save_attendance,
    send_absence_alerts,
    existing_status_map,
)
from ..utils.payment import build_pay_url, fee_reminder_message, monthly_fee_statement
from ..utils.whatsapp import send_whatsapp_message
from ..utils.excel import (
    build_template,
    parse_upload,
    build_marks_template,
    parse_marks_upload,
    build_payments_template,
    parse_payments_upload,
    build_simple_sheet,
)
from ..utils.pdf import render_pdf
from ..utils.exam_analysis import compute_exam_results, build_report_context, student_progress
from ..utils.video import is_acceptable_link
from ..utils.images import read_image_upload
from ..utils.academic_year import active_academic_year_id, active_academic_year
from ..utils.media import save_home_video, delete_media


def _pdf_response(template_name, filename, **context):
    context.setdefault("settings", Settings.get())
    buffer = render_pdf(template_name, **context)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _classes_sorted():
    return visible_classes(sorted(SchoolClass.query.all(), key=lambda c: c.sort_key))


def _subjects_sorted():
    return Subject.query.order_by(Subject.name).all()


def _places_sorted():
    return Place.query.order_by(Place.name).all()


def _schools_sorted():
    return SchoolMaster.query.order_by(SchoolMaster.name).all()


def _place_options(student=None):
    """Master list of place names, plus the student's current value if it
    was since removed from the master (so saving unchanged never loses it)."""
    names = [p.name for p in _places_sorted()]
    if student and student.place and student.place not in names:
        names.append(student.place)
    return names


def _school_options(student=None):
    names = [s.name for s in _schools_sorted()]
    if student and student.school_name and student.school_name not in names:
        names.append(student.school_name)
    return names


# ---------------------------------------------------------------- dashboard
@admin_bp.route("/dashboard")
@login_required
@office_required
def dashboard():
    # An admin may view one branch at a time; an office coordinator is always
    # held to their own branch (resolved_class_ids enforces that).
    branch_id = request.args.get("branch", type=int)
    class_ids = resolved_class_ids(branch_id)

    student_query = Student.query.filter_by(active=True)
    if class_ids is not None:
        student_query = student_query.filter(Student.class_id.in_(class_ids or [-1]))
    students = student_query.all()

    total_expected = sum(s.total_fee for s in students)
    total_collected = sum(s.total_paid for s in students)
    total_pending = round(total_expected - total_collected, 2)

    today = date.today()
    collection_query = FeePayment.query.filter(FeePayment.payment_date == today)
    attend = Attendance.query.filter(Attendance.date == today)
    if class_ids is not None:
        student_ids = [s.id for s in students] or [-1]
        collection_query = collection_query.filter(FeePayment.student_id.in_(student_ids))
        attend = attend.filter(Attendance.student_id.in_(student_ids))

    today_collection = sum(p.amount for p in collection_query.all())
    today_absentees = attend.filter(Attendance.status == "absent").count()
    today_present = attend.filter(Attendance.status == "present").count()

    pending_students = sorted(
        [s for s in students if s.pending_fee > 0], key=lambda s: s.pending_fee, reverse=True
    )[:8]

    return render_template(
        "admin/dashboard.html",
        student_count=len(students),
        teacher_count=Teacher.query.filter_by(active=True).count(),
        total_expected=total_expected,
        total_collected=total_collected,
        total_pending=total_pending,
        today_collection=today_collection,
        today_absentees=today_absentees,
        today_present=today_present,
        pending_students=pending_students,
        classes=visible_classes(_classes_sorted()) if class_ids is None else [
            c for c in _classes_sorted() if c.id in class_ids
        ],
        branches=Branch.query.order_by(Branch.name).all() if current_user.is_admin else [],
        selected_branch_id=branch_id,
    )


# ------------------------------------------------------------------ search
@admin_bp.route("/search")
@login_required
@office_required
def search():
    q = request.args.get("q", "").strip()
    results = []
    if q:
        like = f"%{q}%"
        results = (
            limit_students(
                Student.query.filter(
                    Student.active == True,  # noqa: E712
                    db.or_(
                        Student.name.ilike(like),
                        Student.admission_no.ilike(like),
                        Student.parent_name.ilike(like),
                        Student.parent_whatsapp.ilike(like),
                    ),
                )
            )
            .order_by(Student.name)
            .all()
        )
    return render_template("admin/search_results.html", q=q, results=results)


# ----------------------------------------------------------------- students
def _compute_student_list(args):
    """The filtered student list shared by the page and its downloads."""
    class_id = args.get("class_id", type=int)
    division_id = args.get("division_id", type=int)
    q = args.get("q", "").strip()
    status = args.get("status", "")

    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if division_id:
        query = query.filter_by(division_id=division_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Student.name.ilike(like), Student.admission_no.ilike(like)))

    student_list = query.order_by(Student.name).all()
    if status == "pending":
        student_list = [s for s in student_list if s.pending_fee > 0]
    elif status == "paid":
        student_list = [s for s in student_list if s.pending_fee <= 0]

    # Optional: narrow by how a student did in one exam - e.g. everyone under
    # 35% in the Mid Term, to plan extra sessions.
    exam_id = args.get("exam_id", type=int)
    min_pct = args.get("min_pct", type=float)
    max_pct = args.get("max_pct", type=float)
    scores = {}
    exam = None

    if exam_id:
        exam = Exam.query.get_or_404(exam_id)
        ensure_class_visible(exam.class_id)

        # Rank against the whole class, then filter - otherwise the rank shown
        # would be a position within whatever the other filters left behind.
        whole_class = Student.query.filter_by(class_id=exam.class_id, active=True).all()
        for result in compute_exam_results(exam, whole_class):
            if result["complete"]:
                scores[result["student"].id] = result

        in_class = [s for s in student_list if s.class_id == exam.class_id]

        def keep(student):
            result = scores.get(student.id)
            if result is None:
                return False  # no complete result for this exam
            pct = result["percentage"]
            if min_pct is not None and pct < min_pct:
                return False
            if max_pct is not None and pct > max_pct:
                return False
            return True

        student_list = [s for s in in_class if keep(s)]
        student_list.sort(key=lambda s: scores[s.id]["percentage"], reverse=True)

    exam_options = Exam.query.order_by(Exam.exam_date.desc()).all()
    visible_ids = visible_class_ids()
    if visible_ids is not None:
        exam_options = [e for e in exam_options if e.class_id in visible_ids]

    return {
        "students": student_list,
        "classes": _classes_sorted(),
        "selected_class_id": class_id,
        "selected_division_id": division_id,
        "q": q,
        "status": status,
        "exam_options": exam_options,
        "exam": exam,
        "exam_id": exam_id,
        "min_pct": min_pct,
        "max_pct": max_pct,
        "scores": scores,
    }


def _student_list_label(data):
    """A human description of the current filter, for the report header."""
    bits = []
    for c in data["classes"]:
        if c.id == data["selected_class_id"]:
            bits.append(f"Class {c.name}")
            for d in c.divisions:
                if d.id == data["selected_division_id"]:
                    bits[-1] += f"-{d.name}"
    if not bits:
        bits.append("All classes")
    if data["status"] == "pending":
        bits.append("pending fees")
    elif data["status"] == "paid":
        bits.append("fully paid")
    if data["q"]:
        bits.append(f'search "{data["q"]}"')
    return " \u00b7 ".join(bits)


@admin_bp.route("/students")
@login_required
@office_required
def students():
    return render_template("admin/students.html", **_compute_student_list(request.args))


@admin_bp.route("/students/excel")
@login_required
@office_required
def students_excel():
    data = _compute_student_list(request.args)
    headers = ["Sl No", "Admission No", "Name", "Class", "Division", "Parent Name",
               "Parent WhatsApp", "Place", "School", "Admission Date",
               "Total Fee", "Discount", "Paid", "Pending"]
    rows = []
    for i, st in enumerate(data["students"], start=1):
        rows.append([
            i, st.admission_no, st.name, st.school_class.name, st.division.name,
            st.parent_name or "", st.parent_whatsapp or "", st.place or "",
            st.school_name or "",
            st.admission_date.strftime("%d-%m-%Y") if st.admission_date else "",
            f"{st.total_fee:.0f}", f"{st.discount_amount or 0:.0f}",
            f"{st.total_paid:.0f}", f"{st.pending_fee:.0f}",
        ])
    totals = ["", "", "Total", "", "", "", "", "", "", "",
              f"{sum(s.total_fee for s in data['students']):.0f}", "",
              f"{sum(s.total_paid for s in data['students']):.0f}",
              f"{sum(s.pending_fee for s in data['students']):.0f}"]
    buffer = build_simple_sheet("Students", headers, rows, totals)
    return send_file(buffer, as_attachment=True, download_name="students.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@admin_bp.route("/students/pdf")
@login_required
@office_required
def students_pdf():
    data = _compute_student_list(request.args)
    columns = ["Sl", "Adm No", "Name", "Class-Div", "Parent", "WhatsApp", "Pending"]
    rows = [[i, st.admission_no, st.name,
             f"{st.school_class.name}-{st.division.name}",
             st.parent_name or "-", st.parent_whatsapp or "-",
             f"Rs. {st.pending_fee:,.0f}"]
            for i, st in enumerate(data["students"], start=1)]
    return _pdf_response(
        "pdf/generic_report_pdf.html", "students.pdf",
        title="Student List", meta=_student_list_label(data),
        columns=columns, rows=rows,
        summary_lines=[("Students listed", str(len(data["students"]))),
                       ("Total pending", f"Rs. {sum(s.pending_fee for s in data['students']):,.0f}")],
    )


@admin_bp.route("/students/new", methods=["GET", "POST"])
@admin_bp.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
@office_required
def student_form(student_id=None):
    student = ensure_student_visible(Student.query.get_or_404(student_id)) if student_id else None

    if request.method == "POST":
        division_id = request.form.get("division_id", type=int)
        division = Division.query.get_or_404(division_id)

        admission_no = request.form.get("admission_no", "").strip()
        existing = Student.query.filter_by(admission_no=admission_no).first()
        if existing and (not student or existing.id != student.id):
            flash("That admission number is already in use.", "danger")
            return render_template(
                "admin/student_form.html",
                student=student,
                classes=_classes_sorted(),
                places=_place_options(student),
                school_names=_school_options(student),
            )

        if student is None:
            student = Student(admission_no=admission_no)
            db.session.add(student)

        student.name = request.form.get("name", "").strip()
        student.class_id = division.class_id
        student.division_id = division.id
        student.parent_name = request.form.get("parent_name", "").strip()
        student.parent_whatsapp = request.form.get("parent_whatsapp", "").strip()
        student.address = request.form.get("address", "").strip()
        student.place = request.form.get("place", "").strip()
        student.school_name = request.form.get("school_name", "").strip()
        student.discount_amount = float(request.form.get("discount_amount") or 0)
        student.discount_reason = request.form.get("discount_reason", "").strip()

        dob_raw = request.form.get("dob")
        student.dob = datetime.strptime(dob_raw, "%Y-%m-%d").date() if dob_raw else None
        admission_date_raw = request.form.get("admission_date")
        student.admission_date = (
            datetime.strptime(admission_date_raw, "%Y-%m-%d").date() if admission_date_raw else date.today()
        )

        db.session.commit()
        flash(f"Student {student.name} saved.", "success")
        return redirect(url_for("admin.student_detail", student_id=student.id))

    return render_template(
        "admin/student_form.html",
        student=student,
        classes=_classes_sorted(),
        places=_place_options(student),
        school_names=_school_options(student),
    )


@admin_bp.route("/students/<int:student_id>")
@login_required
@office_required
def student_detail(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    return render_template("admin/student_detail.html", student=student)


@admin_bp.route("/students/<int:student_id>/statement/pdf")
@login_required
@office_required
def student_statement_pdf(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    return _pdf_response(
        "pdf/student_statement_pdf.html",
        f"fee_statement_{student.admission_no}.pdf",
        student=student,
    )


@admin_bp.route("/students/<int:student_id>/deactivate", methods=["POST"])
@login_required
@admin_required
def student_deactivate(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    student.active = False
    db.session.commit()
    flash(f"{student.name} has been deactivated.", "info")
    return redirect(url_for("admin.students"))


@admin_bp.route("/students/<int:student_id>/delete", methods=["GET", "POST"])
@login_required
@admin_required
def student_delete(student_id):
    """Permanently remove a student and everything attached to them.

    This destroys their fee payments, so it is admin-only and asks for the
    admission number to be typed back. Deactivating is offered alongside and is
    what the office should normally use - it hides the student from the lists
    while keeping the money history intact.
    """
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    impact = {
        "payments": len(student.payments),
        "paid": sum(p.amount for p in student.payments),
        "attendance": Attendance.query.filter_by(student_id=student.id).count(),
        "marks": ExamMark.query.filter_by(student_id=student.id).count(),
        "messages": MessageLog.query.filter_by(student_id=student.id).count(),
        "feedback": Feedback.query.filter_by(student_id=student.id).count(),
    }

    if request.method == "POST":
        if request.form.get("confirm", "").strip() != student.admission_no:
            flash("Type the admission number exactly as shown to confirm.", "danger")
            return redirect(url_for("admin.student_delete", student_id=student.id))

        name, adm = student.name, student.admission_no
        # These reference the student without a cascade, so clear them first or
        # the delete fails on a foreign key.
        ExamMark.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        MessageLog.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        Feedback.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        db.session.delete(student)   # payments and attendance cascade
        db.session.commit()
        flash(f"{name} ({adm}) and all their records have been permanently deleted.", "warning")
        return redirect(url_for("admin.students"))

    return render_template("admin/student_delete.html", student=student, impact=impact)


# ------------------------------------------------------------ fee payments
@admin_bp.route("/students/<int:student_id>/payments/add", methods=["POST"])
@login_required
@office_required
def add_payment(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    amount = float(request.form.get("amount") or 0)

    if amount <= 0:
        flash("Payment amount must be greater than zero.", "danger")
        return redirect(url_for("admin.student_detail", student_id=student.id))
    if amount > student.pending_fee + 0.01:
        flash(
            f"Amount exceeds pending balance of ₹{student.pending_fee:,.2f}. Please re-check.",
            "danger",
        )
        return redirect(url_for("admin.student_detail", student_id=student.id))

    payment_date_raw = request.form.get("payment_date")
    payment = FeePayment(
        student_id=student.id,
        amount=amount,
        payment_date=datetime.strptime(payment_date_raw, "%Y-%m-%d").date() if payment_date_raw else date.today(),
        mode=request.form.get("mode", "Cash"),
        remarks=request.form.get("remarks", "").strip(),
        recorded_by=current_user.name,
        academic_year_id=active_academic_year_id(),
    )
    db.session.add(payment)
    db.session.commit()

    payment.receipt_no = f"BW{payment.id:05d}"
    db.session.commit()

    flash(f"Payment of ₹{amount:,.2f} recorded for {student.name}.", "success")
    return redirect(url_for("admin.student_detail", student_id=student.id))


@admin_bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_payment(payment_id):
    payment = FeePayment.query.get_or_404(payment_id)
    student_id = payment.student_id
    db.session.delete(payment)
    db.session.commit()
    flash("Payment entry removed.", "info")
    return redirect(url_for("admin.student_detail", student_id=student_id))


# ----------------------------------------------------------------- teachers
@admin_bp.route("/teachers")
@login_required
@admin_required
def teachers():
    teacher_list = Teacher.query.order_by(Teacher.name).all()
    return render_template("admin/teachers.html", teachers=teacher_list)


@admin_bp.route("/teachers/new", methods=["GET", "POST"])
@admin_bp.route("/teachers/<int:teacher_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def teacher_form(teacher_id=None):
    teacher = Teacher.query.get_or_404(teacher_id) if teacher_id else None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        division_ids = request.form.getlist("division_ids", type=int)
        subject_ids = request.form.getlist("subject_ids", type=int)

        existing_user = User.query.filter_by(username=username).first()
        if existing_user and (not teacher or existing_user.id != teacher.user_id):
            flash("That username is already taken.", "danger")
            return render_template(
                "admin/teacher_form.html", teacher=teacher, classes=_classes_sorted(), subjects=_subjects_sorted()
            )

        if teacher is None:
            if not password:
                flash("Password is required for a new teacher account.", "danger")
                return render_template(
                    "admin/teacher_form.html", teacher=teacher, classes=_classes_sorted(), subjects=_subjects_sorted()
                )
            user = User(username=username, name=name, role="teacher")
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            teacher = Teacher(user_id=user.id, name=name)
            db.session.add(teacher)
        else:
            teacher.user.username = username
            teacher.user.name = name
            teacher.name = name
            if password:
                teacher.user.set_password(password)

        teacher.phone = phone
        teacher.show_on_website = bool(request.form.get("show_on_website"))
        teacher.designation = request.form.get("designation", "").strip()[:120]
        teacher.display_order = request.form.get("display_order", type=int) or 100
        teacher.divisions = Division.query.filter(Division.id.in_(division_ids)).all()
        teacher.subjects = Subject.query.filter(Subject.id.in_(subject_ids)).all()

        db.session.commit()
        flash(f"Teacher {teacher.name} saved.", "success")
        return redirect(url_for("admin.teachers"))

    return render_template(
        "admin/teacher_form.html", teacher=teacher, classes=_classes_sorted(), subjects=_subjects_sorted()
    )


@admin_bp.route("/password-requests")
@login_required
@admin_required
def password_requests():
    return render_template(
        "admin/password_requests.html",
        requests=PasswordResetRequest.query.filter_by(status="pending")
        .order_by(PasswordResetRequest.created_at)
        .all(),
        handled=PasswordResetRequest.query.filter(PasswordResetRequest.status != "pending")
        .order_by(PasswordResetRequest.handled_at.desc())
        .limit(10)
        .all(),
    )


@admin_bp.route("/password-requests/<int:request_id>/reset", methods=["POST"])
@login_required
@admin_required
def password_request_reset(request_id):
    reset_request = PasswordResetRequest.query.get_or_404(request_id)
    new_password = request.form.get("new_password", "")

    if len(new_password) < MIN_PASSWORD_LENGTH:
        flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
        return redirect(url_for("admin.password_requests"))

    reset_request.user.set_password(new_password)
    reset_request.status = "done"
    reset_request.handled_at = datetime.utcnow()
    reset_request.handled_by = current_user.name
    db.session.commit()
    flash(
        f"Password reset for {reset_request.user.name}. Tell them their new password directly.",
        "success",
    )
    return redirect(url_for("admin.password_requests"))


@admin_bp.route("/password-requests/<int:request_id>/dismiss", methods=["POST"])
@login_required
@admin_required
def password_request_dismiss(request_id):
    reset_request = PasswordResetRequest.query.get_or_404(request_id)
    reset_request.status = "dismissed"
    reset_request.handled_at = datetime.utcnow()
    reset_request.handled_by = current_user.name
    db.session.commit()
    flash("Request dismissed.", "info")
    return redirect(url_for("admin.password_requests"))


@admin_bp.route("/teachers/<int:teacher_id>/deactivate", methods=["POST"])
@login_required
@admin_required
def teacher_deactivate(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    teacher.active = False
    teacher.user.active = False
    db.session.commit()
    flash(f"{teacher.name} has been deactivated.", "info")
    return redirect(url_for("admin.teachers"))


@admin_bp.route("/teachers/<int:teacher_id>/activate", methods=["POST"])
@login_required
@admin_required
def teacher_activate(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    teacher.active = True
    teacher.user.active = True
    db.session.commit()
    flash(f"{teacher.name} has been re-activated.", "info")
    return redirect(url_for("admin.teachers"))


# ------------------------------------------------------- classes/divisions
@admin_bp.route("/classes")
@login_required
@admin_required
def classes():
    return render_template(
        "admin/classes.html",
        classes=_classes_sorted(),
        branches=Branch.query.order_by(Branch.name).all(),
        month_names=list(calendar.month_name),
    )


@admin_bp.route("/classes/<int:class_id>/schedule", methods=["POST"])
@login_required
@admin_required
def update_class_schedule(class_id):
    school_class = SchoolClass.query.get_or_404(class_id)
    first = request.form.get("first_installment", type=float)
    monthly = request.form.get("monthly_installment", type=float)
    start = request.form.get("schedule_start_month", type=int)

    school_class.first_installment = max(first or 0, 0)
    school_class.monthly_installment = max(monthly or 0, 0)
    if start and 1 <= start <= 12:
        school_class.schedule_start_month = start
    db.session.commit()
    flash(f"Installment plan updated for Class {school_class.name}.", "success")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/classes/<int:class_id>/branch", methods=["POST"])
@login_required
@admin_required
def update_class_branch(class_id):
    school_class = SchoolClass.query.get_or_404(class_id)
    branch_id = request.form.get("branch_id", type=int)
    school_class.branch_id = branch_id or None
    db.session.commit()
    flash(
        f"Class {school_class.name} moved to "
        f"{school_class.branch.name if school_class.branch else 'no branch'}.",
        "success",
    )
    return redirect(url_for("admin.classes"))


@admin_bp.route("/classes/<int:class_id>/update-fee", methods=["POST"])
@login_required
@admin_required
def update_class_fee(class_id):
    school_class = SchoolClass.query.get_or_404(class_id)
    school_class.base_fee = float(request.form.get("base_fee") or 0)
    db.session.commit()
    flash(f"Base fee for {school_class.name} updated to ₹{school_class.base_fee:,.2f}.", "success")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/classes/<int:class_id>/divisions/add", methods=["POST"])
@login_required
@admin_required
def add_division(class_id):
    school_class = SchoolClass.query.get_or_404(class_id)
    name = request.form.get("name", "").strip().upper()
    if not name:
        flash("Division name is required.", "danger")
    elif Division.query.filter_by(class_id=class_id, name=name).first():
        flash(f"Division {name} already exists for {school_class.name}.", "danger")
    else:
        db.session.add(Division(name=name, class_id=class_id))
        db.session.commit()
        flash(f"Division {name} added to {school_class.name}.", "success")
    return redirect(url_for("admin.classes"))


@admin_bp.route("/divisions/<int:division_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_division(division_id):
    division = Division.query.get_or_404(division_id)
    if division.students:
        flash("Cannot delete a division that still has students.", "danger")
    else:
        db.session.delete(division)
        db.session.commit()
        flash("Division removed.", "info")
    return redirect(url_for("admin.classes"))


# --------------------------------------------------------------- reports
def _compute_finance_report(args):
    period = args.get("period", "daily")
    today = date.today()

    if period == "monthly":
        year = args.get("year", type=int) or today.year
        month = args.get("month", type=int) or today.month
        start = date(year, month, 1)
        end = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
        label = start.strftime("%B %Y")
    elif period == "range":
        start_raw = args.get("start")
        end_raw = args.get("end")
        start = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else today.replace(day=1)
        end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else today
        year, month = start.year, start.month
        label = f"{start.strftime('%d-%m-%Y')} to {end.strftime('%d-%m-%Y')}"
    else:
        period = "daily"
        date_raw = args.get("date")
        start = end = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else today
        year, month = start.year, start.month
        label = start.strftime("%d-%m-%Y")

    q = args.get("q", "").strip()
    branch_id = args.get("branch", type=int)
    class_ids = resolved_class_ids(branch_id)

    payments_query = FeePayment.query.filter(
        FeePayment.payment_date >= start, FeePayment.payment_date <= end
    )
    # Keep each branch's collection to itself - an office coordinator must not
    # see the other branch's receipts, and an admin can pick one branch.
    payments_query = scope_payments(payments_query, class_ids)
    year_id = active_academic_year_id()
    if year_id:
        payments_query = payments_query.filter(
            db.or_(FeePayment.academic_year_id == year_id, FeePayment.academic_year_id.is_(None)))
    if q:
        like = f"%{q}%"
        payments_query = payments_query.join(Student).filter(
            db.or_(Student.name.ilike(like), Student.admission_no.ilike(like))
        )
    payments = payments_query.order_by(FeePayment.payment_date, FeePayment.created_at).all()
    total = sum(p.amount for p in payments)

    by_mode = {}
    by_class = {}
    for p in payments:
        by_mode[p.mode] = by_mode.get(p.mode, 0) + p.amount
        class_name = p.student.school_class.name
        by_class[class_name] = by_class.get(class_name, 0) + p.amount

    return {
        "period": period,
        "start": start,
        "end": end,
        "label": label,
        "year": year,
        "month": month,
        "q": q,
        "payments": payments,
        "total": total,
        "by_mode": by_mode,
        "by_class": by_class,
        "branches": Branch.query.order_by(Branch.name).all() if current_user.is_admin else [],
        "selected_branch_id": branch_id,
    }


@admin_bp.route("/reports/finance")
@login_required
@office_required
def finance_report():
    return render_template("admin/finance_report.html", **_compute_finance_report(request.args))


@admin_bp.route("/reports/finance/pdf")
@login_required
@office_required
def finance_report_pdf():
    data = _compute_finance_report(request.args)
    columns = ["Date", "Receipt", "Student", "Class", "Amount", "Mode", "Recorded By"]
    rows = [
        [
            p.payment_date.strftime("%d-%m-%Y"),
            p.receipt_no or "-",
            p.student.name,
            f"{p.student.school_class.name}-{p.student.division.name}",
            f"Rs. {p.amount:,.0f}",
            p.mode,
            p.recorded_by or "-",
        ]
        for p in data["payments"]
    ]
    totals_row = ["", "", "", "Total", f"Rs. {data['total']:,.0f}", "", ""]
    summary_lines = [("Total Collected", f"Rs. {data['total']:,.0f}")]
    for mode, amount in data["by_mode"].items():
        summary_lines.append((mode, f"Rs. {amount:,.0f}"))

    return _pdf_response(
        "pdf/generic_report_pdf.html",
        f"fee_collection_{data['period']}_{data['start'].isoformat()}.pdf",
        title="Fee Collection Report",
        meta=data["label"],
        columns=columns,
        rows=rows,
        totals_row=totals_row,
        summary_lines=summary_lines,
    )


def _compute_class_wise_report():
    today = date.today()
    rows = []
    for school_class in _classes_sorted():
        class_students = [s for s in school_class.students if s.active]
        student_ids = [s.id for s in class_students]
        expected = sum(s.total_fee for s in class_students)
        collected = sum(s.total_paid for s in class_students)
        if student_ids:
            present_today = Attendance.query.filter(
                Attendance.date == today,
                Attendance.status == "present",
                Attendance.student_id.in_(student_ids),
            ).count()
            absent_today = Attendance.query.filter(
                Attendance.date == today,
                Attendance.status == "absent",
                Attendance.student_id.in_(student_ids),
            ).count()
        else:
            present_today = absent_today = 0

        rows.append(
            {
                "school_class": school_class,
                "student_count": len(class_students),
                "expected": expected,
                "collected": collected,
                "pending": round(expected - collected, 2),
                "present_today": present_today,
                "absent_today": absent_today,
            }
        )
    return rows


@admin_bp.route("/reports/class-wise")
@login_required
@office_required
def class_wise_report():
    return render_template("admin/class_report.html", rows=_compute_class_wise_report())


@admin_bp.route("/reports/class-wise/pdf")
@login_required
@office_required
def class_wise_report_pdf():
    rows = _compute_class_wise_report()
    columns = ["Class", "Students", "Expected Fee", "Collected", "Pending", "Present Today", "Absent Today"]
    table_rows = [
        [
            f"Class {r['school_class'].name}",
            r["student_count"],
            f"Rs. {r['expected']:,.0f}",
            f"Rs. {r['collected']:,.0f}",
            f"Rs. {r['pending']:,.0f}",
            r["present_today"],
            r["absent_today"],
        ]
        for r in rows
    ]
    totals_row = [
        "Total",
        sum(r["student_count"] for r in rows),
        f"Rs. {sum(r['expected'] for r in rows):,.0f}",
        f"Rs. {sum(r['collected'] for r in rows):,.0f}",
        f"Rs. {sum(r['pending'] for r in rows):,.0f}",
        "",
        "",
    ]
    return _pdf_response(
        "pdf/generic_report_pdf.html",
        "class_wise_report.pdf",
        title="Class-wise Report",
        meta=None,
        columns=columns,
        rows=table_rows,
        totals_row=totals_row,
        summary_lines=None,
    )


def _compute_student_wise_report(args):
    class_id = args.get("class_id", type=int)
    division_id = args.get("division_id", type=int)
    q = args.get("q", "").strip()

    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if division_id:
        query = query.filter_by(division_id=division_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Student.name.ilike(like), Student.admission_no.ilike(like)))

    student_list = query.order_by(Student.name).all()
    totals = {
        "expected": sum(s.total_fee for s in student_list),
        "collected": sum(s.total_paid for s in student_list),
        "pending": sum(s.pending_fee for s in student_list),
    }
    return student_list, class_id, division_id, q, totals


@admin_bp.route("/reports/student-wise")
@login_required
@office_required
def student_wise_report():
    student_list, class_id, division_id, q, totals = _compute_student_wise_report(request.args)
    return render_template(
        "admin/student_wise_report.html",
        students=student_list,
        classes=_classes_sorted(),
        selected_class_id=class_id,
        selected_division_id=division_id,
        q=q,
        totals=totals,
    )


@admin_bp.route("/reports/student-wise/pdf")
@login_required
@office_required
def student_wise_report_pdf():
    student_list, class_id, division_id, q, totals = _compute_student_wise_report(request.args)
    columns = ["Admission No.", "Name", "Class", "Total Fee", "Paid", "Pending", "Status"]
    rows = [
        [
            s.admission_no,
            s.name,
            f"{s.school_class.name}-{s.division.name}",
            f"Rs. {s.total_fee:,.0f}",
            f"Rs. {s.total_paid:,.0f}",
            f"Rs. {s.pending_fee:,.0f}",
            s.payment_status,
        ]
        for s in student_list
    ]
    totals_row = [
        "", "Total", "",
        f"Rs. {totals['expected']:,.0f}",
        f"Rs. {totals['collected']:,.0f}",
        f"Rs. {totals['pending']:,.0f}",
        "",
    ]
    return _pdf_response(
        "pdf/generic_report_pdf.html",
        "student_wise_report.pdf",
        title="Student-wise Report",
        meta=None,
        columns=columns,
        rows=rows,
        totals_row=totals_row,
        summary_lines=None,
    )


def _compute_pending_fees_report(args):
    class_id = args.get("class_id", type=int)
    division_id = args.get("division_id", type=int)
    q = args.get("q", "").strip()
    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if division_id:
        query = query.filter_by(division_id=division_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Student.name.ilike(like), Student.admission_no.ilike(like)))

    student_list = [s for s in query.all() if s.pending_fee > 0]
    student_list.sort(key=lambda s: s.pending_fee, reverse=True)
    total_pending = sum(s.pending_fee for s in student_list)
    return student_list, class_id, division_id, q, total_pending


@admin_bp.route("/reports/pending-fees")
@login_required
@office_required
def pending_fees_report():
    student_list, class_id, division_id, q, total_pending = _compute_pending_fees_report(request.args)
    return render_template(
        "admin/pending_fees.html",
        students=student_list,
        classes=_classes_sorted(),
        selected_class_id=class_id,
        selected_division_id=division_id,
        q=q,
        total_pending=total_pending,
    )


@admin_bp.route("/reports/pending-fees/pdf")
@login_required
@office_required
def pending_fees_report_pdf():
    student_list, class_id, division_id, q, total_pending = _compute_pending_fees_report(request.args)
    columns = ["Admission No.", "Name", "Class", "Parent WhatsApp", "Total Fee", "Paid", "Pending"]
    rows = [
        [
            s.admission_no,
            s.name,
            f"{s.school_class.name}-{s.division.name}",
            s.parent_whatsapp,
            f"Rs. {s.total_fee:,.0f}",
            f"Rs. {s.total_paid:,.0f}",
            f"Rs. {s.pending_fee:,.0f}",
        ]
        for s in student_list
    ]
    totals_row = ["", "", "", "Total", "", "", f"Rs. {total_pending:,.0f}"]
    return _pdf_response(
        "pdf/generic_report_pdf.html",
        "pending_fees_report.pdf",
        title="Pending Fees Report",
        meta=None,
        columns=columns,
        rows=rows,
        totals_row=totals_row,
        summary_lines=[("Total Pending", f"Rs. {total_pending:,.0f}"), ("Students", len(student_list))],
    )


def _queue_fee_reminder(student):
    pay_url = build_pay_url(student.id)
    message = fee_reminder_message(student, pay_url)
    result = send_whatsapp_message(student.parent_whatsapp, message)
    db.session.add(
        MessageLog(
            student_id=student.id,
            date=date.today(),
            category="fee_reminder",
            message=message,
            phone=student.parent_whatsapp,
            status=result["status"],
            detail=result["detail"],
            manual_link=result.get("link"),
        )
    )


@admin_bp.route("/students/<int:student_id>/send-fee-reminder", methods=["POST"])
@login_required
@office_required
def send_fee_reminder(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    if student.pending_fee <= 0:
        flash(f"{student.name} has no pending fee.", "info")
    else:
        _queue_fee_reminder(student)
        db.session.commit()
        flash(f"Fee reminder queued for {student.name}'s parent on WhatsApp.", "success")
    return redirect(request.referrer or url_for("admin.student_detail", student_id=student.id))


@admin_bp.route("/reports/pending-fees/send-reminders", methods=["POST"])
@login_required
@office_required
def send_bulk_fee_reminders():
    class_id = request.form.get("class_id", type=int)
    division_id = request.form.get("division_id", type=int)
    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if division_id:
        query = query.filter_by(division_id=division_id)
    pending_students = [s for s in query.all() if s.pending_fee > 0]

    for student in pending_students:
        _queue_fee_reminder(student)
    db.session.commit()

    flash(
        f"Fee reminders queued for {len(pending_students)} student(s). "
        f"Check the Messages page to send/verify each one on WhatsApp.",
        "success",
    )
    return redirect(url_for("admin.pending_fees_report", class_id=class_id) if class_id else url_for("admin.pending_fees_report"))


@admin_bp.route("/reports/discounts")
@login_required
@office_required
def discount_report():
    student_list = (
        limit_students(Student.query.filter(Student.active == True, Student.discount_amount > 0))  # noqa: E712
        .order_by(Student.discount_amount.desc())
        .all()
    )
    total_discount = sum(s.discount_amount for s in student_list)
    return render_template(
        "admin/discount_report.html", students=student_list, total_discount=total_discount
    )


@admin_bp.route("/reports/attendance")
@login_required
@office_required
def attendance_report():
    report_date_raw = request.args.get("date")
    report_date = (
        datetime.strptime(report_date_raw, "%Y-%m-%d").date() if report_date_raw else date.today()
    )
    class_ids = visible_class_ids()
    records_q = Attendance.query.filter_by(date=report_date).join(Student)
    if class_ids is not None:
        records_q = records_q.filter(Student.class_id.in_(class_ids or [-1]))
    records = records_q.order_by(Student.name).all()
    present = [r for r in records if r.status == "present"]
    absent = [r for r in records if r.status == "absent"]

    by_division = {}
    for division in visible_divisions(Division.query.all()):
        active_count = sum(1 for s in division.students if s.active)
        if active_count == 0:
            continue
        marked = [r for r in records if r.student.division_id == division.id]
        by_division[division] = {
            "total": active_count,
            "present": sum(1 for r in marked if r.status == "present"),
            "absent": sum(1 for r in marked if r.status == "absent"),
            # Counted by student, not by row: in twice-daily mode one student
            # contributes two rows, which would otherwise read as two students
            # marked and push this negative.
            "unmarked": active_count - len({r.student_id for r in marked}),
        }

    return render_template(
        "admin/attendance_report.html",
        report_date=report_date,
        present=present,
        absent=absent,
        by_division=by_division,
        settings=Settings.get(),
        students_marked=len({r.student_id for r in records}),
        daily_sent=_daily_summary_sent_on(report_date),
    )


# ---------------------------------------------------------------- messages
@admin_bp.route("/messages")
@login_required
@office_required
def messages():
    q = request.args.get("q", "").strip()
    query = MessageLog.query.order_by(MessageLog.created_at.desc())
    if q:
        like = f"%{q}%"
        query = query.join(Student).filter(
            db.or_(Student.name.ilike(like), Student.admission_no.ilike(like), MessageLog.phone.ilike(like))
        )
    logs = query.limit(200).all()
    pending_count = MessageLog.query.filter_by(status="manual").count()
    return render_template("admin/messages.html", logs=logs, pending_count=pending_count, q=q)


@admin_bp.route("/attendance/mark", methods=["GET", "POST"])
@login_required
@office_required
def attendance_mark():
    """Office coordinator's register - any division, any session."""
    settings = Settings.get()
    divisions = visible_divisions(
        sorted(Division.query.all(), key=lambda d: (d.school_class.sort_key, d.name))
    )
    if not divisions:
        flash("No class divisions are available for your branch.", "warning")
        return redirect(url_for("admin.dashboard"))

    division_id = request.values.get("division_id", type=int) or divisions[0].id
    division = ensure_division_visible(Division.query.get_or_404(division_id))

    date_raw = request.values.get("att_date")
    att_date = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else date.today()
    session = resolve_session(request.values.get("session"), settings)

    students = sorted([s for s in division.students if s.active], key=lambda s: s.name)

    if request.method == "POST":
        absentees = save_attendance(
            division, att_date, session, students, request.form, current_user.name
        )
        alerted = send_absence_alerts(absentees, division, att_date, session, settings)
        label = "" if session == "full" else f" ({session_label(session)})"
        note = (
            f"{alerted} WhatsApp alert(s) queued."
            if alerted
            else "Instant absence alerts are switched off."
        )
        flash(
            f"Attendance saved for {division.display_name}{label} on "
            f"{att_date.strftime('%d-%m-%Y')}. {len(absentees)} absentee(s). {note}",
            "success",
        )
        return redirect(
            url_for(
                "admin.attendance_mark",
                division_id=division.id,
                att_date=att_date.isoformat(),
                session=session,
            )
        )

    return render_template(
        "admin/attendance_mark.html",
        divisions=divisions,
        division=division,
        students=students,
        att_date=att_date,
        session=session,
        settings=settings,
        existing_map=existing_status_map(division.id, att_date, session),
    )


def _daily_summary_sent_on(att_date):
    """The log row for a daily summary already sent for this date, if any."""
    return (
        MessageLog.query.filter_by(category="daily_attendance", date=att_date)
        .order_by(MessageLog.created_at.desc())
        .first()
    )


@admin_bp.route("/attendance/send-daily", methods=["POST"])
@login_required
@office_required
def attendance_send_daily():
    date_raw = request.form.get("att_date")
    att_date = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else date.today()

    already = _daily_summary_sent_on(att_date)
    if already:
        flash(
            f"Today's attendance was already sent to parents at "
            f"{already.created_at.strftime('%d-%m-%Y %I:%M %p')}.",
            "warning",
        )
        return redirect(url_for("admin.attendance_report", date=att_date.isoformat()))

    class_ids = visible_class_ids()
    records_q = Attendance.query.filter_by(date=att_date)
    if class_ids is not None:
        records_q = records_q.join(Student).filter(Student.class_id.in_(class_ids or [-1]))
    records = records_q.all()
    if not records:
        flash("No attendance has been marked for that date yet.", "warning")
        return redirect(url_for("admin.attendance_report", date=att_date.isoformat()))

    by_student = {}
    for record in records:
        by_student.setdefault(record.student_id, []).append(record)

    settings_obj = Settings.get()
    sent = failed = manual = 0

    for student_id, rows in by_student.items():
        student = db.session.get(Student, student_id)
        if student is None or not student.active:
            continue

        # Chronological, not alphabetical: 'an' sorts before 'fn' but the
        # afternoon does not come first.
        order = {"fn": 0, "an": 1, "full": 2}
        rows.sort(key=lambda r: order.get(r.session, 9))

        if len(rows) == 1 and rows[0].session == "full":
            body = "was PRESENT" if rows[0].status == "present" else "was ABSENT"
        else:
            parts = [f"{session_label(r.session)} {r.status.upper()}" for r in rows]
            body = " - ".join(parts)

        message = (
            f"Dear Parent, attendance for {student.name} (Class "
            f"{student.school_class.name}-{student.division.name}) on "
            f"{att_date.strftime('%d-%m-%Y')}: {body}. - {settings_obj.academy_name}"
        )

        result = send_whatsapp_message(student.parent_whatsapp, message)
        db.session.add(
            MessageLog(
                student_id=student.id,
                date=att_date,
                category="daily_attendance",
                message=message,
                phone=student.parent_whatsapp,
                status=result["status"],
                detail=result["detail"],
                manual_link=result["link"],
            )
        )
        if result["status"] == "sent":
            sent += 1
        elif result["status"] == "failed":
            failed += 1
        else:
            manual += 1

    db.session.commit()

    if manual:
        flash(
            f"Prepared {manual} daily attendance message(s). Twilio is not configured, so "
            "tap 'Send on WhatsApp' against each on the Messages page.",
            "info",
        )
    if sent:
        flash(f"Sent {sent} daily attendance message(s) to parents.", "success")
    if failed:
        flash(f"{failed} message(s) failed - see the Messages page.", "danger")
    return redirect(url_for("admin.attendance_report", date=att_date.isoformat()))


@admin_bp.route("/students/<int:student_id>/progress")
@login_required
@office_required
def student_progress_report(student_id):
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    exams = Exam.query.filter_by(class_id=student.class_id).all()
    return render_template(
        "admin/student_progress.html",
        student=student,
        progress=student_progress(student, exams),
    )


@admin_bp.route("/settings/test-whatsapp", methods=["POST"])
@login_required
@admin_required
def settings_test_whatsapp():
    """Send one message to a number the admin types, to prove the connection.

    Deliberately not to a parent: the point is to check credentials without
    anyone's family receiving a test message.
    """
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("Enter a mobile number to send the test to.", "danger")
        return redirect(url_for("admin.settings"))

    result = send_whatsapp_message(
        phone,
        f"Test message from {Settings.get().academy_name}. "
        "If you are reading this, WhatsApp sending is working.",
    )

    if result["status"] == "sent":
        flash(f"Test message sent to {phone}. Check that phone.", "success")
    elif result["status"] == "failed":
        flash(f"Twilio rejected it: {result['detail']}", "danger")
    else:
        flash(
            "Twilio is not configured, so nothing was sent automatically. "
            "Use this link to send it by hand: " + result["link"],
            "info",
        )
    return redirect(url_for("admin.settings"))


@admin_bp.route("/payments/bulk-upload", methods=["GET", "POST"])
@login_required
@office_required
def payments_bulk_upload():
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a filled payments sheet (.xlsx) to upload.", "danger")
            return redirect(url_for("admin.payments_bulk_upload"))

        try:
            rows = parse_payments_upload(upload)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin.payments_bulk_upload"))

        students = {
            s.admission_no.strip().lower(): s
            for s in limit_students(Student.query.filter_by(active=True)).all()
        }

        saved = skipped = 0
        total = 0.0
        errors = []

        for row in rows:
            admission_no = str(row.get("admission_no", "")).strip()
            student = students.get(admission_no.lower())
            if student is None:
                errors.append(f"Row {row['_row']}: no active student {admission_no}.")
                continue

            # Track the balance across this row: three installments of 2,000
            # against a 5,000 balance must not all go through.
            remaining = student.pending_fee
            existing = {
                (round(p.amount, 2), p.payment_date) for p in student.payments
            }

            for entry in row["payments"]:
                label = f"Row {row['_row']} installment {entry['n']}"

                try:
                    amount = float(entry["amount"])
                except (TypeError, ValueError):
                    errors.append(f"{label}: '{entry['amount']}' is not an amount.")
                    continue
                if amount <= 0:
                    errors.append(f"{label}: amount must be more than zero.")
                    continue

                raw_date = entry.get("date")
                try:
                    if isinstance(raw_date, datetime):
                        paid_on = raw_date.date()
                    elif isinstance(raw_date, date):
                        paid_on = raw_date
                    elif raw_date:
                        paid_on = datetime.strptime(str(raw_date).strip()[:10], "%Y-%m-%d").date()
                    else:
                        paid_on = date.today()
                except ValueError:
                    errors.append(f"{label}: '{raw_date}' is not a date (use YYYY-MM-DD).")
                    continue

                # Re-uploading a sheet is a realistic mistake; without this it
                # would double every payment on it.
                if (round(amount, 2), paid_on) in existing:
                    skipped += 1
                    continue

                if amount > remaining:
                    errors.append(
                        f"{label}: only Rs. {remaining:,.0f} left on {student.name}'s balance "
                        f"- Rs. {amount:,.0f} rejected."
                    )
                    continue

                mode = (str(entry.get("mode") or "Cash")).strip() or "Cash"
                payment = FeePayment(
                    student_id=student.id,
                    amount=amount,
                    payment_date=paid_on,
                    mode=mode,
                    remarks="Bulk upload",
                    recorded_by=current_user.name,
                    academic_year_id=active_academic_year_id(),
                )
                db.session.add(payment)
                db.session.flush()
                payment.receipt_no = f"BW{payment.id:05d}"

                existing.add((round(amount, 2), paid_on))
                remaining -= amount
                saved += 1
                total += amount

        db.session.commit()

        if saved:
            flash(f"Recorded {saved} payment(s) totalling Rs. {total:,.0f}.", "success")
        if skipped:
            flash(
                f"{skipped} payment(s) were already recorded with the same amount and date, "
                "so they were not added again.",
                "info",
            )
        for message in errors[:12]:
            flash(message, "warning")
        if len(errors) > 12:
            flash(f"...and {len(errors) - 12} more problem(s) not shown.", "warning")
        if not saved and not errors and not skipped:
            flash("Nothing to record - no installments were filled in.", "info")
        return redirect(url_for("admin.payments_bulk_upload"))

    return render_template("admin/payments_bulk_upload.html", classes=_classes_sorted(),
                           selected_class_id=request.args.get("class_id", type=int),
                           selected_division_id=request.args.get("division_id", type=int))


@admin_bp.route("/payments/bulk-upload/template")
@login_required
@office_required
def payments_bulk_template():
    query = limit_students(Student.query.filter_by(active=True))
    class_id = request.args.get("class_id", type=int)
    division_id = request.args.get("division_id", type=int)
    if class_id:
        ensure_class_visible(class_id)
        query = query.filter_by(class_id=class_id)
    if division_id:
        query = query.filter_by(division_id=division_id)

    installments = min(max(request.args.get("installments", type=int) or 6, 1), 12)
    students = [s for s in query.order_by(Student.name).all() if s.pending_fee > 0]
    buffer = build_payments_template(students, installments)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="fee_payments.xlsx",
    )


@admin_bp.route("/feedback")
@login_required
@office_required
def feedback():
    show = request.args.get("show", "open")
    query = Feedback.query.join(Student)
    class_ids = visible_class_ids()
    if class_ids is not None:
        query = query.filter(Student.class_id.in_(class_ids or [-1]))
    if show in ("open", "answered"):
        query = query.filter(Feedback.status == show)

    return render_template(
        "admin/feedback.html",
        threads=query.order_by(Feedback.created_at.desc()).limit(200).all(),
        show=show,
    )


@admin_bp.route("/feedback/<int:feedback_id>/reply", methods=["POST"])
@login_required
@office_required
def feedback_reply(feedback_id):
    thread = Feedback.query.get_or_404(feedback_id)
    ensure_student_visible(thread.student)

    reply = request.form.get("reply", "").strip()
    if not reply:
        flash("Type a reply before sending.", "danger")
        return redirect(url_for("admin.feedback"))

    thread.reply = reply[:2000]
    thread.status = "answered"
    thread.replied_by = current_user.name
    thread.replied_at = datetime.utcnow()
    db.session.commit()

    # The parent sees the reply in the portal; WhatsApp only tells them to look,
    # so a private message is not copied into an unencrypted channel.
    if request.form.get("notify"):
        student = thread.student
        result = send_whatsapp_message(
            student.parent_whatsapp,
            f"Dear Parent, the academy office has replied to your message about "
            f"{student.name}. Please open the student portal to read it.",
        )
        db.session.add(
            MessageLog(
                student_id=student.id,
                category="broadcast",
                message="Feedback reply notification",
                phone=student.parent_whatsapp,
                status=result["status"],
                detail=result["detail"],
                manual_link=result["link"],
            )
        )
        db.session.commit()

    flash("Reply sent - the parent will see it in the portal.", "success")
    return redirect(url_for("admin.feedback"))


@admin_bp.route("/audit-log")
@login_required
@admin_required
def audit_log():
    entity = request.args.get("entity", "").strip()
    actor = request.args.get("actor", "").strip()

    query = AuditLog.query.order_by(AuditLog.created_at.desc())
    if entity:
        query = query.filter_by(entity_type=entity)
    if actor:
        query = query.filter(AuditLog.actor_name.ilike(f"%{actor}%"))

    entries = query.limit(300).all()
    entity_types = [
        row[0]
        for row in db.session.query(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type)
    ]
    return render_template(
        "admin/audit_log.html",
        entries=entries,
        entity_types=entity_types,
        entity=entity,
        actor=actor,
    )


@admin_bp.route("/office-staff", methods=["GET", "POST"])
@login_required
@admin_required
def office_staff():
    """Front-office coordinator logins. Admin-only: these accounts can record
    money, so who holds one is the administrator's decision."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")

        branch_id = request.form.get("branch_id", type=int)

        if not username or not name or len(password) < MIN_PASSWORD_LENGTH:
            flash(
                "Username, name and a password of at least "
                f"{MIN_PASSWORD_LENGTH} characters are required.",
                "danger",
            )
        elif not branch_id:
            flash("Choose which branch this coordinator runs.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.", "danger")
        else:
            user = User(username=username, name=name, role="office", branch_id=branch_id)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Office login created for {name}.", "success")
        return redirect(url_for("admin.office_staff"))

    return render_template(
        "admin/office_staff.html",
        staff=User.query.filter_by(role="office").order_by(User.name).all(),
        branches=Branch.query.order_by(Branch.name).all(),
    )


@admin_bp.route("/office-staff/<int:user_id>/branch", methods=["POST"])
@login_required
@admin_required
def office_staff_branch(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != "office":
        abort(404)
    branch_id = request.form.get("branch_id", type=int)
    if not branch_id or not Branch.query.get(branch_id):
        flash("Choose a branch.", "danger")
    else:
        user.branch_id = branch_id
        db.session.commit()
        flash(f"{user.name} now runs {user.branch.name}.", "success")
    return redirect(url_for("admin.office_staff"))


@admin_bp.route("/office-staff/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def office_staff_toggle(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != "office":
        abort(404)
    user.active = not user.active
    db.session.commit()
    flash(f"{user.name} has been {'activated' if user.active else 'deactivated'}.", "info")
    return redirect(url_for("admin.office_staff"))


@admin_bp.route("/office-staff/<int:user_id>/password", methods=["POST"])
@login_required
@admin_required
def office_staff_password(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != "office":
        abort(404)
    password = request.form.get("password", "")
    if len(password) < 4:
        flash("Password must be at least 4 characters.", "danger")
    else:
        user.set_password(password)
        db.session.commit()
        flash(f"Password updated for {user.name}.", "success")
    return redirect(url_for("admin.office_staff"))


@admin_bp.route("/videos")
@login_required
@admin_required
def videos():
    all_videos = VideoClass.query.order_by(VideoClass.created_at.desc()).all()
    return render_template(
        "admin/videos.html",
        pending=[v for v in all_videos if v.approval == "pending"],
        reviewed=[v for v in all_videos if v.approval != "pending"],
    )


@admin_bp.route("/videos/<int:video_id>/review", methods=["POST"])
@login_required
@admin_required
def video_review(video_id):
    video = VideoClass.query.get_or_404(video_id)
    decision = request.form.get("decision")
    if decision not in ("approved", "rejected"):
        abort(400)

    video.approval = decision
    video.reviewed_by = current_user.name
    video.reviewed_at = datetime.utcnow()
    video.review_note = request.form.get("review_note", "").strip()[:255]
    db.session.commit()

    if decision == "approved":
        flash(f'"{video.title}" approved - students can now see it.', "success")
    else:
        flash(f'"{video.title}" rejected. The teacher will see this on their Videos page.', "info")
    return redirect(url_for("admin.videos"))


@admin_bp.route("/videos/<int:video_id>/delete", methods=["POST"])
@login_required
@admin_required
def video_delete(video_id):
    video = VideoClass.query.get_or_404(video_id)
    db.session.delete(video)
    db.session.commit()
    flash(f'"{video.title}" removed.', "info")
    return redirect(url_for("admin.videos"))


def _broadcast_recipients(scope, class_id, division_id):
    """Active students matching the chosen audience, with a label for the UI."""
    query = limit_students(Student.query.filter_by(active=True))

    if scope == "division" and division_id:
        division = ensure_division_visible(Division.query.get_or_404(division_id))
        return query.filter_by(division_id=division.id).all(), division.display_name
    if scope == "class" and class_id:
        school_class = SchoolClass.query.get_or_404(class_id)
        ensure_class_visible(school_class.id)
        return query.filter_by(class_id=school_class.id).all(), f"Class {school_class.name}"

    branch = current_branch()
    return query.all(), f"All students in {branch.name}" if branch else "All students"


@admin_bp.route("/broadcast", methods=["GET", "POST"])
@login_required
@office_required
def broadcast():
    classes = _classes_sorted()
    divisions = visible_divisions(Division.query.join(SchoolClass).all())

    settings_obj = Settings.get()

    if request.method == "POST":
        template = request.form.get("message", "").strip()
        scope = request.form.get("scope", "all")
        class_id = request.form.get("class_id", type=int)
        division_id = request.form.get("division_id", type=int)
        kind = request.form.get("kind", "custom")
        fee_statement = kind == "fee_statement"

        as_of_raw = request.form.get("as_of", "")
        as_of = (
            datetime.strptime(as_of_raw, "%Y-%m-%d").date() if as_of_raw else date.today()
        )

        if not fee_statement and not template:
            flash("Please type a message to send.", "danger")
            return redirect(url_for("admin.broadcast"))

        recipients, audience = _broadcast_recipients(scope, class_id, division_id)
        if not recipients:
            flash("No active students match that selection.", "warning")
            return redirect(url_for("admin.broadcast"))

        settled = 0
        if fee_statement:
            include_ontrack = bool(request.form.get("include_ontrack"))
            # Telling a family who has paid in full that their balance is zero
            # invites a worried phone call, so leave them out and say how many.
            # By default those keeping up with the installment plan are left out
            # too - a reminder is for people who are behind.
            def needs_reminder(student):
                if student.pending_fee <= 0:
                    return False
                if include_ontrack:
                    return True
                due = student.school_class.scheduled_due(as_of, student.total_fee)
                return round(due - student.total_paid, 2) > 0

            wanted = [s for s in recipients if needs_reminder(s)]
            settled = len(recipients) - len(wanted)
            recipients = wanted
            if not recipients:
                flash(
                    f"Nobody in {audience} is behind on the plan for "
                    f"{as_of.strftime('%B %Y')} - nothing to send.",
                    "info",
                )
                return redirect(url_for("admin.broadcast"))

        sent = failed = manual = 0
        for student in recipients:
            if fee_statement:
                message = monthly_fee_statement(
                    student,
                    build_pay_url(student.id),
                    as_of,
                    settings_obj.academy_name,
                )
                if template:
                    message += f"\n\n{template}"
            else:
                message = (
                    template.replace("{name}", student.name)
                    .replace("{class}", f"{student.school_class.name}-{student.division.name}")
                    .replace("{pending}", f"{student.pending_fee:,.0f}")
                )
            result = send_whatsapp_message(student.parent_whatsapp, message)
            db.session.add(
                MessageLog(
                    student_id=student.id,
                    category="fee_reminder" if fee_statement else "broadcast",
                    message=message,
                    phone=student.parent_whatsapp,
                    status=result["status"],
                    detail=result["detail"],
                    manual_link=result["link"],
                )
            )
            if result["status"] == "sent":
                sent += 1
            elif result["status"] == "failed":
                failed += 1
            else:
                manual += 1

        db.session.commit()

        if manual:
            flash(
                f"Prepared {manual} message(s) for {audience}. Twilio is not configured, so "
                "tap 'Send on WhatsApp' against each one below.",
                "info",
            )
        if sent:
            flash(f"Sent {sent} message(s) automatically to {audience}.", "success")
        if failed:
            flash(f"{failed} message(s) failed - see the details below.", "danger")
        if settled:
            flash(
                f"{settled} student(s) in {audience} were not messaged - either paid in full "
                "or up to date with the installment plan.",
                "info",
            )
        return redirect(url_for("admin.messages"))

    pending_counts = {
        "all": sum(
            1 for s in limit_students(Student.query.filter_by(active=True)).all() if s.pending_fee > 0
        ),
    }

    counts = {
        "all": limit_students(Student.query.filter_by(active=True)).count(),
        "classes": {
            c.id: limit_students(Student.query.filter_by(active=True, class_id=c.id)).count()
            for c in classes
        },
        "divisions": {
            d.id: limit_students(Student.query.filter_by(active=True, division_id=d.id)).count()
            for d in divisions
        },
    }
    return render_template(
        "admin/broadcast.html",
        classes=classes,
        divisions=divisions,
        counts=counts,
        pending_counts=pending_counts,
        settings=settings_obj,
        today=date.today(),
        preset=request.args.get("kind", "custom"),
    )


@admin_bp.route("/messages/<int:message_id>/mark-sent", methods=["POST"])
@login_required
@office_required
def mark_message_sent(message_id):
    log = MessageLog.query.get_or_404(message_id)
    log.status = "sent"
    log.detail = f"Marked as sent manually by {current_user.name}"
    db.session.commit()
    return redirect(url_for("admin.messages"))


# ---------------------------------------------------------------- settings

def _safe_social_url(raw):
    """A public, clickable URL, or empty.

    Only http(s) is allowed through: the value lands in an href shown to
    every public visitor, so a 'javascript:' or 'data:' scheme must never
    be stored. A bare 'instagram.com/brainwave' is treated as https.
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    # Parse before touching the string: urlparse reads "javascript:alert(1)"
    # as scheme=javascript, so a dangerous scheme is caught here rather than
    # being disguised by a prepended "https://".
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in ('http', 'https'):
        return ''
    if not parsed.scheme:
        raw = 'https://' + raw
        parsed = urlparse(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return ''
    return raw[:255]


@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    settings_obj = Settings.get()
    if request.method == "POST":
        settings_obj.academy_name = request.form.get("academy_name", "").strip() or "Brainwave Academy"
        settings_obj.tagline = request.form.get("tagline", "").strip()
        settings_obj.address = request.form.get("address", "").strip()
        settings_obj.phone = request.form.get("phone", "").strip()
        settings_obj.email = request.form.get("email", "").strip()
        settings_obj.instagram_url = _safe_social_url(request.form.get("instagram_url"))
        settings_obj.facebook_url = _safe_social_url(request.form.get("facebook_url"))
        settings_obj.youtube_url = _safe_social_url(request.form.get("youtube_url"))
        settings_obj.upi_id = request.form.get("upi_id", "").strip()
        settings_obj.upi_payee_name = request.form.get("upi_payee_name", "").strip()
        settings_obj.student_login_enabled = bool(request.form.get("student_login_enabled"))
        sessions_mode = request.form.get("attendance_sessions", "single")
        settings_obj.attendance_sessions = sessions_mode if sessions_mode in ("single", "twice") else "single"
        settings_obj.instant_absence_alert = bool(request.form.get("instant_absence_alert"))

        upload = request.files.get("logo")
        if request.form.get("remove_logo"):
            settings_obj.logo_data = None
            settings_obj.logo_mimetype = None
        elif upload and upload.filename:
            data = upload.read()
            # PDF reports are rendered by xhtml2pdf, which cannot draw SVG, so
            # a vector upload would leave every report without a logo.
            if upload.mimetype not in ("image/png", "image/jpeg"):
                flash("Logo must be a PNG or JPG image. SVG files cannot be used in PDF reports.", "danger")
                return redirect(url_for("admin.settings"))
            if len(data) > 2 * 1024 * 1024:
                flash("Logo must be smaller than 2 MB.", "danger")
                return redirect(url_for("admin.settings"))
            settings_obj.logo_data = data
            settings_obj.logo_mimetype = upload.mimetype

        db.session.commit()
        flash("Settings updated.", "success")
        return redirect(url_for("admin.settings"))
    return render_template(
        "admin/settings.html",
        settings=settings_obj,
        whatsapp={
            "configured": bool(
                current_app.config.get("TWILIO_ACCOUNT_SID")
                and current_app.config.get("TWILIO_AUTH_TOKEN")
                and current_app.config.get("TWILIO_WHATSAPP_FROM")
            ),
            "from_number": current_app.config.get("TWILIO_WHATSAPP_FROM", ""),
            "sandbox": current_app.config.get("TWILIO_WHATSAPP_FROM", "").endswith("14155238886"),
        },
    )


# ---------------------------------------------------------- bulk upload
@admin_bp.route("/students/bulk-upload", methods=["GET", "POST"])
@login_required
@office_required
def students_bulk_upload():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Please choose an Excel (.xlsx) file to upload.", "danger")
            return redirect(url_for("admin.students_bulk_upload"))

        try:
            rows = parse_upload(file.stream)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin.students_bulk_upload"))

        classes_by_name = {c.name.strip().lower(): c for c in SchoolClass.query.all()}
        existing_admission_nos = {
            a.lower() for (a,) in db.session.query(Student.admission_no).all()
        }
        places_by_name = {p.name.lower(): p for p in Place.query.all()}
        schools_by_name = {s.name.lower(): s for s in SchoolMaster.query.all()}
        created = []
        errors = []

        for record in rows:
            row_no = record.get("_row")
            missing = [
                field
                for field in ("admission_no", "name", "class", "division", "parent_whatsapp")
                if not str(record.get(field) or "").strip()
            ]
            if missing:
                errors.append(f"Row {row_no}: missing required field(s) - {', '.join(missing)}.")
                continue

            admission_no = str(record["admission_no"]).strip()
            if admission_no.lower() in existing_admission_nos:
                errors.append(f"Row {row_no}: admission number '{admission_no}' already exists or is repeated in the sheet.")
                continue

            class_name = str(record["class"]).strip()
            school_class = classes_by_name.get(class_name.lower())
            if not school_class:
                valid = ", ".join(c.name for c in _classes_sorted())
                errors.append(f"Row {row_no}: unknown class '{class_name}'. Valid classes: {valid}.")
                continue

            division_name = str(record["division"]).strip().upper()
            division = next((d for d in school_class.divisions if d.name.upper() == division_name), None)
            if division is None:
                division = Division(name=division_name, class_id=school_class.id)
                db.session.add(division)
                db.session.flush()
                school_class.divisions.append(division)

            place_name = str(record.get("place") or "").strip()
            if place_name and place_name.lower() not in places_by_name:
                new_place = Place(name=place_name)
                db.session.add(new_place)
                db.session.flush()
                places_by_name[place_name.lower()] = new_place

            school_name = str(record.get("school_name") or "").strip()
            if school_name and school_name.lower() not in schools_by_name:
                new_school = SchoolMaster(name=school_name)
                db.session.add(new_school)
                db.session.flush()
                schools_by_name[school_name.lower()] = new_school

            student = Student(
                admission_no=admission_no,
                name=str(record["name"]).strip(),
                class_id=school_class.id,
                division_id=division.id,
                parent_name=str(record.get("parent_name") or "").strip(),
                parent_whatsapp=str(record["parent_whatsapp"]).strip(),
                address=str(record.get("address") or "").strip(),
                place=place_name,
                school_name=school_name,
                dob=_parse_flexible_date(record.get("dob")),
                admission_date=_parse_flexible_date(record.get("admission_date")) or date.today(),
                discount_reason=str(record.get("discount_reason") or "").strip(),
            )

            try:
                student.discount_amount = float(record.get("discount_amount") or 0)
            except (TypeError, ValueError):
                errors.append(f"Row {row_no}: invalid discount_amount, defaulted to 0.")
                student.discount_amount = 0

            db.session.add(student)
            existing_admission_nos.add(admission_no.lower())
            created.append(student)

        if created:
            db.session.commit()
        else:
            db.session.rollback()

        flash(
            f"{len(created)} student(s) added successfully." if created else "No students were added - see the errors below.",
            "success" if created else "warning",
        )
        return render_template(
            "admin/students_bulk_upload.html",
            classes=_classes_sorted(),
            results=True,
            created=created,
            errors=errors,
        )

    return render_template("admin/students_bulk_upload.html", classes=_classes_sorted(), results=False)


def _parse_flexible_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@admin_bp.route("/students/bulk-upload/template")
@login_required
@office_required
def students_bulk_upload_template():
    class_names = [c.name for c in _classes_sorted()]
    buffer = build_template(class_names)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="brainwave_students_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ----------------------------------------------------------------- masters
@admin_bp.route("/masters")
@login_required
@office_required
def masters():
    return render_template(
        "admin/masters.html",
        subjects=_subjects_sorted(),
        places=_places_sorted(),
        schools=_schools_sorted(),
    )


@admin_bp.route("/masters/subjects/add", methods=["POST"])
@login_required
@admin_required
def add_subject():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Subject name is required.", "danger")
    elif Subject.query.filter(db.func.lower(Subject.name) == name.lower()).first():
        flash(f"Subject '{name}' already exists.", "danger")
    else:
        db.session.add(Subject(name=name))
        db.session.commit()
        flash(f"Subject '{name}' added.", "success")
    return redirect(url_for("admin.masters"))


@admin_bp.route("/masters/subjects/<int:subject_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if subject.teachers:
        flash(f"Cannot delete '{subject.name}' - it is assigned to {len(subject.teachers)} teacher(s).", "danger")
    else:
        db.session.delete(subject)
        db.session.commit()
        flash(f"Subject '{subject.name}' removed.", "info")
    return redirect(url_for("admin.masters"))


@admin_bp.route("/masters/places/add", methods=["POST"])
@login_required
@office_required
def add_place():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Place name is required.", "danger")
    elif Place.query.filter(db.func.lower(Place.name) == name.lower()).first():
        flash(f"Place '{name}' already exists.", "danger")
    else:
        db.session.add(Place(name=name))
        db.session.commit()
        flash(f"Place '{name}' added.", "success")
    return redirect(url_for("admin.masters"))


@admin_bp.route("/masters/places/<int:place_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_place(place_id):
    place = Place.query.get_or_404(place_id)
    db.session.delete(place)
    db.session.commit()
    flash(f"Place '{place.name}' removed from the list.", "info")
    return redirect(url_for("admin.masters"))


@admin_bp.route("/masters/schools/add", methods=["POST"])
@login_required
@office_required
def add_school():
    name = request.form.get("name", "").strip()
    if not name:
        flash("School name is required.", "danger")
    elif SchoolMaster.query.filter(db.func.lower(SchoolMaster.name) == name.lower()).first():
        flash(f"School '{name}' already exists.", "danger")
    else:
        db.session.add(SchoolMaster(name=name))
        db.session.commit()
        flash(f"School '{name}' added.", "success")
    return redirect(url_for("admin.masters"))


@admin_bp.route("/masters/schools/<int:school_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_school(school_id):
    school = SchoolMaster.query.get_or_404(school_id)
    db.session.delete(school)
    db.session.commit()
    flash(f"School '{school.name}' removed from the list.", "info")
    return redirect(url_for("admin.masters"))


# ------------------------------------------------------------------ exams
@admin_bp.route("/exams")
@login_required
@office_required
def exams():
    query = Exam.query.order_by(Exam.exam_date.desc())
    year_id = active_academic_year_id()
    if year_id:
        # Show this year's exams, plus any legacy exam with no year set.
        query = query.filter(db.or_(Exam.academic_year_id == year_id, Exam.academic_year_id.is_(None)))
    exam_list = query.all()
    class_ids = visible_class_ids()
    if class_ids is not None:
        exam_list = [e for e in exam_list if e.class_id in class_ids]
    return render_template("admin/exams.html", exams=exam_list, classes=_classes_sorted())


@admin_bp.route("/exams/new", methods=["GET", "POST"])
@login_required
@admin_required
def exam_form():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        class_id = request.form.get("class_id", type=int)
        exam_date_raw = request.form.get("exam_date")
        description = request.form.get("description", "").strip()

        if not name or not class_id:
            flash("Exam name and class are required.", "danger")
            return render_template("admin/exam_form.html", classes=_classes_sorted())

        exam = Exam(
            name=name,
            class_id=class_id,
            exam_date=datetime.strptime(exam_date_raw, "%Y-%m-%d").date() if exam_date_raw else date.today(),
            description=description,
            academic_year_id=active_academic_year_id(),
        )
        db.session.add(exam)
        db.session.commit()
        flash(f"Exam '{exam.name}' created. Now add its subjects below.", "success")
        return redirect(url_for("admin.exam_detail", exam_id=exam.id))

    return render_template("admin/exam_form.html", classes=_classes_sorted())


@admin_bp.route("/exams/<int:exam_id>")
@login_required
@office_required
def exam_detail(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    return render_template("admin/exam_detail.html", exam=exam, subjects=_subjects_sorted())


@admin_bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    db.session.delete(exam)
    db.session.commit()
    flash(f"Exam '{exam.name}' and all its marks have been deleted.", "info")
    return redirect(url_for("admin.exams"))


@admin_bp.route("/exams/<int:exam_id>/subjects/add", methods=["POST"])
@login_required
@admin_required
def add_exam_subject(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    subject_id = request.form.get("subject_id", type=int)
    max_marks = request.form.get("max_marks", type=float) or 100
    pass_marks = request.form.get("pass_marks", type=float) or 35

    if not subject_id:
        flash("Please choose a subject.", "danger")
    elif ExamSubject.query.filter_by(exam_id=exam.id, subject_id=subject_id).first():
        flash("That subject is already part of this exam.", "danger")
    else:
        db.session.add(ExamSubject(exam_id=exam.id, subject_id=subject_id, max_marks=max_marks, pass_marks=pass_marks))
        db.session.commit()
        flash("Subject added to exam.", "success")
    return redirect(url_for("admin.exam_detail", exam_id=exam.id))


@admin_bp.route("/exams/<int:exam_id>/subjects/<int:exam_subject_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_exam_subject(exam_id, exam_subject_id):
    """Change a subject's max and pass marks after it was added to the exam."""
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    exam_subject = ExamSubject.query.filter_by(id=exam_subject_id, exam_id=exam.id).first_or_404()

    max_marks = request.form.get("max_marks", type=float)
    pass_marks = request.form.get("pass_marks", type=float)

    if max_marks is None or max_marks <= 0:
        flash("Max mark must be a number greater than zero.", "danger")
    elif pass_marks is None or pass_marks < 0:
        flash("Pass mark must be zero or more.", "danger")
    elif pass_marks > max_marks:
        flash("Pass mark cannot be higher than the max mark.", "danger")
    else:
        # Lowering the max below a mark already entered would leave that student
        # scoring more than the subject allows, so refuse rather than hide it.
        over = [m for m in exam_subject.marks if m.marks_obtained > max_marks]
        if over:
            flash(
                f"Cannot set max to {max_marks:g}: {len(over)} student(s) already have "
                f"more than that recorded. Correct those marks first.",
                "danger",
            )
        else:
            exam_subject.max_marks = max_marks
            exam_subject.pass_marks = pass_marks
            db.session.commit()
            flash(f"{exam_subject.subject.name}: max and pass marks updated.", "success")
    return redirect(url_for("admin.exam_detail", exam_id=exam.id))


@admin_bp.route("/exams/<int:exam_id>/subjects/<int:exam_subject_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_exam_subject(exam_id, exam_subject_id):
    exam_subject = ExamSubject.query.filter_by(id=exam_subject_id, exam_id=exam_id).first_or_404()
    db.session.delete(exam_subject)
    db.session.commit()
    flash("Subject removed from exam (its marks were removed too).", "info")
    return redirect(url_for("admin.exam_detail", exam_id=exam_id))


def _exam_students(exam, division_id=None):
    query = Student.query.filter_by(class_id=exam.class_id, active=True)
    if division_id:
        query = query.filter_by(division_id=division_id)
    return query.order_by(Student.name).all()


@admin_bp.route("/exams/<int:exam_id>/marks/template")
@login_required
@office_required
def exam_marks_template(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    if not exam.exam_subjects:
        flash("Add the exam's subjects before downloading the marks sheet.", "warning")
        return redirect(url_for("admin.exam_detail", exam_id=exam.id))

    students = _exam_students(exam, request.args.get("division_id", type=int))
    existing = {
        (mark.student_id, mark.exam_subject_id): mark.marks_obtained
        for es in exam.exam_subjects
        for mark in es.marks
    }
    buffer = build_marks_template(exam, students, existing)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"marks_{exam.name.replace(' ', '_')}_{exam.school_class.name}.xlsx",
    )


@admin_bp.route("/exams/<int:exam_id>/marks/upload", methods=["POST"])
@login_required
@office_required
def exam_marks_upload(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)

    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a filled marks sheet (.xlsx) to upload.", "danger")
        return redirect(url_for("admin.exam_detail", exam_id=exam.id))

    try:
        rows, _ = parse_marks_upload(upload)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin.exam_detail", exam_id=exam.id))

    subjects_by_name = {es.subject.name.strip().lower(): es for es in exam.exam_subjects}
    students_by_admission = {
        s.admission_no.strip().lower(): s for s in _exam_students(exam)
    }

    saved = skipped = 0
    errors = []

    for row in rows:
        student = students_by_admission.get(row["admission_no"].lower())
        if student is None:
            skipped += 1
            errors.append(f"Row {row['_row']}: no student {row['admission_no']} in this class.")
            continue

        for subject_name, raw in row["marks"].items():
            exam_subject = subjects_by_name.get(subject_name.strip().lower())
            if exam_subject is None:
                errors.append(f"Row {row['_row']}: '{subject_name}' is not a subject of this exam.")
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                errors.append(f"Row {row['_row']}: '{raw}' is not a number for {subject_name}.")
                continue
            if value < 0 or value > exam_subject.max_marks:
                errors.append(
                    f"Row {row['_row']}: {subject_name} must be between 0 and "
                    f"{exam_subject.max_marks:g} (got {value:g})."
                )
                continue

            mark = ExamMark.query.filter_by(
                exam_subject_id=exam_subject.id, student_id=student.id
            ).first()
            if mark:
                mark.marks_obtained = value
                mark.entered_by = current_user.name
            else:
                db.session.add(
                    ExamMark(
                        exam_subject_id=exam_subject.id,
                        student_id=student.id,
                        marks_obtained=value,
                        entered_by=current_user.name,
                    )
                )
            saved += 1

    db.session.commit()

    if saved:
        flash(f"Saved {saved} mark(s) from the uploaded sheet.", "success")
    if skipped:
        flash(f"{skipped} row(s) skipped - the student is not in this class.", "warning")
    for message in errors[:12]:
        flash(message, "warning")
    if len(errors) > 12:
        flash(f"...and {len(errors) - 12} more problem(s) not shown.", "warning")
    if not saved and not errors:
        flash("Nothing to save - every marks cell in the sheet was blank.", "info")

    return redirect(url_for("admin.exam_detail", exam_id=exam.id))


@admin_bp.route("/exams/<int:exam_id>/marks", methods=["GET", "POST"])
@login_required
@office_required
def exam_marks(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    if not exam.exam_subjects:
        flash("Add at least one subject to this exam before entering marks.", "warning")
        return redirect(url_for("admin.exam_detail", exam_id=exam.id))

    division_id = request.values.get("division_id", type=int) or (
        exam.school_class.divisions[0].id if exam.school_class.divisions else None
    )
    exam_subject_id = request.values.get("exam_subject_id", type=int) or exam.exam_subjects[0].id
    exam_subject = ExamSubject.query.filter_by(id=exam_subject_id, exam_id=exam.id).first_or_404()

    students = _exam_students(exam, division_id)

    if request.method == "POST":
        for student in students:
            raw = request.form.get(f"marks_{student.id}", "").strip()
            existing = ExamMark.query.filter_by(exam_subject_id=exam_subject.id, student_id=student.id).first()
            if raw == "":
                if existing:
                    db.session.delete(existing)
                continue
            try:
                value = float(raw)
            except ValueError:
                flash(f"Ignored invalid marks for {student.name}.", "warning")
                continue
            value = max(0, min(value, exam_subject.max_marks))
            if existing:
                existing.marks_obtained = value
                existing.entered_by = current_user.name
            else:
                db.session.add(
                    ExamMark(
                        exam_subject_id=exam_subject.id,
                        student_id=student.id,
                        marks_obtained=value,
                        entered_by=current_user.name,
                    )
                )
        db.session.commit()
        flash(f"Marks saved for {exam_subject.subject.name}.", "success")
        return redirect(url_for("admin.exam_marks", exam_id=exam.id, division_id=division_id, exam_subject_id=exam_subject.id))

    existing_marks = {
        m.student_id: m.marks_obtained
        for m in ExamMark.query.filter_by(exam_subject_id=exam_subject.id).all()
    }

    return render_template(
        "admin/exam_marks.html",
        exam=exam,
        exam_subject=exam_subject,
        divisions=exam.school_class.divisions,
        selected_division_id=division_id,
        students=students,
        existing_marks=existing_marks,
    )


def _exam_report_context(exam, division_id=None):
    students = _exam_students(exam, division_id)
    context = build_report_context(exam, students)
    context.update({"exam": exam, "division_id": division_id, "divisions": exam.school_class.divisions})
    return context


@admin_bp.route("/exams/<int:exam_id>/report")
@login_required
@office_required
def exam_report(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    division_id = request.args.get("division_id", type=int)
    return render_template("admin/exam_report.html", **_exam_report_context(exam, division_id))


@admin_bp.route("/exams/<int:exam_id>/report/pdf")
@login_required
@office_required
def exam_report_pdf(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    division_id = request.args.get("division_id", type=int)
    context = _exam_report_context(exam, division_id)
    return _pdf_response(
        "pdf/exam_analysis_pdf.html",
        f"exam_analysis_{exam.name.replace(' ', '_')}.pdf",
        **context,
    )


@admin_bp.route("/exams/<int:exam_id>/report/excel")
@login_required
@office_required
def exam_report_excel(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    division_id = request.args.get("division_id", type=int)
    students = _exam_students(exam, division_id)
    results = compute_exam_results(exam, students)

    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Marks"

    headers = ["Rank", "Admission No.", "Name", "Class"] + [es.subject.name for es in exam.exam_subjects] + [
        "Total", "Max", "Percentage", "Grade", "Status"
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in results:
        row = [r["rank"] or "-", r["student"].admission_no, r["student"].name,
               f"{r['student'].school_class.name}-{r['student'].division.name}"]
        for es in exam.exam_subjects:
            mark = r["marks_by_subject"].get(es.id)
            row.append(mark.marks_obtained if mark else "")
        row += [r["total_obtained"], r["total_max"], r["percentage"], r["grade"], r["status"]]
        ws.append(row)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"exam_marks_{exam.name.replace(' ', '_')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@admin_bp.route("/exams/<int:exam_id>/students/<int:student_id>/report-card")
@login_required
@office_required
def exam_report_card_pdf(exam_id, student_id):
    exam = Exam.query.get_or_404(exam_id)
    ensure_class_visible(exam.class_id)
    student = ensure_student_visible(Student.query.get_or_404(student_id))
    result = compute_exam_results(exam, [student])[0]
    return _pdf_response(
        "pdf/report_card_pdf.html",
        f"report_card_{student.admission_no}_{exam.name.replace(' ', '_')}.pdf",
        exam=exam,
        student=student,
        result=result,
    )


# ------------------------------------------------- subscription video courses
@admin_bp.route("/courses")
@login_required
@admin_required
def courses():
    return render_template(
        "admin/courses.html",
        courses=Course.query.order_by(Course.title).all(),
    )


@admin_bp.route("/courses/new", methods=["POST"])
@admin_bp.route("/courses/<int:course_id>/edit", methods=["POST"])
@login_required
@admin_required
def course_form(course_id=None):
    course = Course.query.get_or_404(course_id) if course_id else Course()
    title = request.form.get("title", "").strip()
    if not title:
        flash("A course needs a title.", "danger")
        return redirect(url_for("admin.courses"))

    course.title = title
    course.subject_label = request.form.get("subject_label", "").strip()
    course.description = request.form.get("description", "").strip()
    course.price = max(request.form.get("price", type=float) or 0, 0)
    course.duration_days = max(request.form.get("duration_days", type=int) or 30, 1)
    course.published = bool(request.form.get("published"))

    thumb_data, thumb_type, thumb_error = read_image_upload(request.files.get("thumbnail"))
    if thumb_error:
        flash(thumb_error, "danger")
        return redirect(url_for("admin.courses"))
    if thumb_data:
        course.thumbnail_data = thumb_data
        course.thumbnail_mimetype = thumb_type

    if course_id is None:
        db.session.add(course)
    db.session.commit()
    flash(f'"{course.title}" saved.', "success")
    return redirect(url_for("admin.course_detail", course_id=course.id))


@admin_bp.route("/courses/<int:course_id>")
@login_required
@admin_required
def course_detail(course_id):
    course = Course.query.get_or_404(course_id)
    return render_template("admin/course_detail.html", course=course)


@admin_bp.route("/courses/<int:course_id>/videos/add", methods=["POST"])
@login_required
@admin_required
def course_video_add(course_id):
    course = Course.query.get_or_404(course_id)
    title = request.form.get("title", "").strip()
    url = request.form.get("url", "").strip()

    if not title or not url:
        flash("A lecture needs a title and a link.", "danger")
    elif not is_acceptable_link(url):
        flash("The link must start with http:// or https://", "danger")
    else:
        thumb_data, thumb_type, thumb_error = read_image_upload(request.files.get("thumbnail"))
        if thumb_error:
            flash(thumb_error, "danger")
            return redirect(url_for("admin.course_detail", course_id=course.id))

        next_seq = max([v.sequence for v in course.videos], default=0) + 1
        db.session.add(
            CourseVideo(
                course_id=course.id,
                title=title,
                url=url,
                thumbnail_data=thumb_data,
                thumbnail_mimetype=thumb_type,
                description=request.form.get("description", "").strip()[:500],
                sequence=request.form.get("sequence", type=int) or next_seq,
            )
        )
        db.session.commit()
        flash(f'"{title}" added.', "success")
    return redirect(url_for("admin.course_detail", course_id=course.id))


@admin_bp.route("/courses/videos/<int:video_id>/delete", methods=["POST"])
@login_required
@admin_required
def course_video_delete(video_id):
    video = CourseVideo.query.get_or_404(video_id)
    course_id = video.course_id
    db.session.delete(video)
    db.session.commit()
    flash("Lecture removed.", "info")
    return redirect(url_for("admin.course_detail", course_id=course_id))


@admin_bp.route("/subscriptions")
@login_required
@office_required
def subscriptions():
    show = request.args.get("show", "pending")
    query = Subscription.query
    if show == "pending":
        query = query.filter_by(status="pending")
    elif show == "active":
        query = query.filter(Subscription.status == "active")

    rows = query.order_by(Subscription.requested_at.desc()).limit(200).all()
    if show == "active":
        rows = [r for r in rows if r.is_current]

    return render_template(
        "admin/subscriptions.html",
        rows=rows,
        show=show,
        pending_total=Subscription.query.filter_by(status="pending").count(),
    )


@admin_bp.route("/subscriptions/<int:subscription_id>/decide", methods=["POST"])
@login_required
@office_required
def subscription_decide(subscription_id):
    subscription = Subscription.query.get_or_404(subscription_id)
    decision = request.form.get("decision")
    if decision not in ("activate", "reject"):
        abort(400)

    subscription.office_note = request.form.get("office_note", "").strip()[:255]
    subscription.activated_by = current_user.name
    subscription.activated_at = datetime.utcnow()

    if decision == "activate":
        # Runs from today rather than the request date, so a subscriber does not
        # lose the days spent waiting for the office to check the bank.
        subscription.status = "active"
        subscription.starts_on = date.today()
        subscription.expires_on = date.today() + timedelta(
            days=subscription.course.duration_days
        )
        db.session.commit()
        flash(
            f"{subscription.subscriber.name} now has access to "
            f"{subscription.course.title} until "
            f"{subscription.expires_on.strftime('%d-%m-%Y')}.",
            "success",
        )
    else:
        subscription.status = "rejected"
        db.session.commit()
        flash("Marked as not activated. The subscriber will see your note.", "info")

    return redirect(url_for("admin.subscriptions"))


# ------------------------------------------- course questions & assignments
@admin_bp.route("/course-questions")
@login_required
@admin_required
def course_questions():
    show = request.args.get("show", "open")
    query = CourseQuestion.query
    if show == "open":
        query = query.filter(CourseQuestion.answer.is_(None))
    elif show == "answered":
        query = query.filter(CourseQuestion.answer.isnot(None))

    return render_template(
        "admin/course_questions.html",
        questions=query.order_by(CourseQuestion.created_at.desc()).limit(200).all(),
        show=show,
        open_total=CourseQuestion.query.filter(CourseQuestion.answer.is_(None)).count(),
    )


@admin_bp.route("/course-questions/<int:question_id>/answer", methods=["POST"])
@login_required
@admin_required
def course_question_answer(question_id):
    question = CourseQuestion.query.get_or_404(question_id)
    answer = request.form.get("answer", "").strip()
    if not answer:
        flash("Type an answer before sending.", "danger")
        return redirect(url_for("admin.course_questions"))

    question.answer = answer[:4000]
    question.answered_by = current_user.name
    question.answered_at = datetime.utcnow()
    db.session.commit()
    flash(
        "Answer published - everyone subscribed to this course can now read it.",
        "success",
    )
    return redirect(url_for("admin.course_questions"))


@admin_bp.route("/courses/<int:course_id>/assignments", methods=["GET", "POST"])
@login_required
@admin_required
def course_assignments(course_id):
    course = Course.query.get_or_404(course_id)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("An assignment needs a title.", "danger")
            return redirect(url_for("admin.course_assignments", course_id=course.id))

        due_raw = request.form.get("due_on", "")
        due_on = datetime.strptime(due_raw, "%Y-%m-%d").date() if due_raw else None

        db.session.add(
            Assignment(
                course_id=course.id,
                title=title,
                instructions=request.form.get("instructions", "").strip(),
                max_marks=max(request.form.get("max_marks", type=float) or 10, 1),
                due_on=due_on,
                published=bool(request.form.get("published")),
            )
        )
        db.session.commit()
        flash(f'"{title}" added.', "success")
        return redirect(url_for("admin.course_assignments", course_id=course.id))

    return render_template(
        "admin/course_assignments.html",
        course=course,
        assignments=Assignment.query.filter_by(course_id=course.id)
        .order_by(Assignment.created_at.desc())
        .all(),
    )


@admin_bp.route("/assignments/<int:assignment_id>/submissions")
@login_required
@admin_required
def assignment_submissions(assignment_id):
    assignment = Assignment.query.get_or_404(assignment_id)
    rows = sorted(assignment.submissions, key=lambda s: (s.is_evaluated, s.submitted_at))
    return render_template(
        "admin/assignment_submissions.html", assignment=assignment, submissions=rows
    )


@admin_bp.route("/submissions/<int:submission_id>/evaluate", methods=["POST"])
@login_required
@admin_required
def submission_evaluate(submission_id):
    submission = AssignmentSubmission.query.get_or_404(submission_id)
    marks = request.form.get("marks", type=float)
    maximum = submission.assignment.max_marks

    if marks is None or marks < 0 or marks > maximum:
        flash(f"Marks must be between 0 and {maximum:g}.", "danger")
    else:
        submission.marks = marks
        submission.remarks = request.form.get("remarks", "").strip()[:2000]
        submission.evaluated_by = current_user.name
        submission.evaluated_at = datetime.utcnow()
        db.session.commit()
        flash(f"{submission.subscriber.name}'s work marked {marks:g}/{maximum:g}.", "success")

    return redirect(
        url_for("admin.assignment_submissions", assignment_id=submission.assignment_id)
    )


@admin_bp.route("/submissions/<int:submission_id>/attachment")
@login_required
@admin_required
def submission_attachment_admin(submission_id):
    submission = AssignmentSubmission.query.get_or_404(submission_id)
    if not submission.attachment_data:
        abort(404)
    return send_file(
        BytesIO(submission.attachment_data),
        mimetype=submission.attachment_mimetype or "image/png",
    )


# ------------------------------------------------------------------ gallery
@admin_bp.route("/gallery", methods=["GET", "POST"])
@login_required
@office_required
def gallery():
    """Admin and office staff add photos to the public gallery."""
    if request.method == "POST":
        data, mimetype, error = read_image_upload(request.files.get("image"))
        if error:
            flash(error, "danger")
            return redirect(url_for("admin.gallery"))
        if data is None:
            flash("Choose an image to add.", "danger")
            return redirect(url_for("admin.gallery"))
        db.session.add(GalleryImage(
            image_data=data,
            image_mimetype=mimetype,
            caption=request.form.get("caption", "").strip()[:200],
            uploaded_by=current_user.name,
        ))
        db.session.commit()
        flash("Photo added to the gallery.", "success")
        return redirect(url_for("admin.gallery"))

    images = GalleryImage.query.order_by(GalleryImage.created_at.desc()).all()
    return render_template("admin/gallery.html", images=images)


@admin_bp.route("/gallery/<int:image_id>/toggle", methods=["POST"])
@login_required
@office_required
def gallery_toggle(image_id):
    image = GalleryImage.query.get_or_404(image_id)
    image.published = not image.published
    db.session.commit()
    flash("Gallery updated.", "success")
    return redirect(url_for("admin.gallery"))


@admin_bp.route("/gallery/<int:image_id>/delete", methods=["POST"])
@login_required
@office_required
def gallery_delete(image_id):
    image = GalleryImage.query.get_or_404(image_id)
    db.session.delete(image)
    db.session.commit()
    flash("Photo removed from the gallery.", "success")
    return redirect(url_for("admin.gallery"))


# ------------------------------------------------------------------ banners
@admin_bp.route("/banners", methods=["GET", "POST"])
@login_required
@admin_required
def banners():
    """Manage the rotating image banner on the public home page."""
    if request.method == "POST":
        data, mimetype, error = read_image_upload(request.files.get("image"))
        if error:
            flash(error, "danger")
            return redirect(url_for("admin.banners"))
        if data is None:
            flash("Choose an image to add.", "danger")
            return redirect(url_for("admin.banners"))
        last = db.session.query(func.max(BannerImage.sort_order)).scalar() or 0
        db.session.add(BannerImage(
            image_data=data,
            image_mimetype=mimetype,
            caption=request.form.get("caption", "").strip()[:200],
            link_url=request.form.get("link_url", "").strip()[:255],
            sort_order=last + 10,
        ))
        db.session.commit()
        flash("Banner added.", "success")
        return redirect(url_for("admin.banners"))

    images = BannerImage.query.order_by(BannerImage.sort_order, BannerImage.created_at).all()
    return render_template("admin/banners.html", images=images)


@admin_bp.route("/banners/<int:banner_id>/update", methods=["POST"])
@login_required
@admin_required
def banner_update(banner_id):
    banner = BannerImage.query.get_or_404(banner_id)
    banner.caption = request.form.get("caption", "").strip()[:200]
    banner.link_url = request.form.get("link_url", "").strip()[:255]
    banner.sort_order = request.form.get("sort_order", type=int) or banner.sort_order
    db.session.commit()
    flash("Banner updated.", "success")
    return redirect(url_for("admin.banners"))


@admin_bp.route("/banners/<int:banner_id>/toggle", methods=["POST"])
@login_required
@admin_required
def banner_toggle(banner_id):
    banner = BannerImage.query.get_or_404(banner_id)
    banner.active = not banner.active
    db.session.commit()
    flash("Banner updated.", "success")
    return redirect(url_for("admin.banners"))


@admin_bp.route("/banners/<int:banner_id>/delete", methods=["POST"])
@login_required
@admin_required
def banner_delete(banner_id):
    banner = BannerImage.query.get_or_404(banner_id)
    db.session.delete(banner)
    db.session.commit()
    flash("Banner removed.", "success")
    return redirect(url_for("admin.banners"))


# ------------------------------------------------------------------ team
def _save_member_photo(member, remove_flag, upload):
    """Apply a photo change to a team member; returns an error string or None."""
    if remove_flag:
        member.photo_data = None
        member.photo_mimetype = None
        return None
    data, mimetype, error = read_image_upload(upload)
    if error:
        return error
    if data is not None:
        member.photo_data = data
        member.photo_mimetype = mimetype
    return None


@admin_bp.route("/team", methods=["GET", "POST"])
@login_required
@admin_required
def team():
    """Manage the management / leadership team shown on the home page."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return redirect(url_for("admin.team"))
        last = db.session.query(func.max(TeamMember.sort_order)).scalar() or 0
        member = TeamMember(
            name=name[:120],
            designation=request.form.get("designation", "").strip()[:120],
            credentials=request.form.get("credentials", "").strip()[:200],
            bio=request.form.get("bio", "").strip(),
            sort_order=last + 10,
        )
        error = _save_member_photo(member, False, request.files.get("photo"))
        if error:
            flash(error, "danger")
            return redirect(url_for("admin.team"))
        db.session.add(member)
        db.session.commit()
        flash("Team member added.", "success")
        return redirect(url_for("admin.team"))

    members = TeamMember.query.order_by(TeamMember.sort_order, TeamMember.created_at).all()
    return render_template("admin/team.html", members=members)


@admin_bp.route("/team/<int:member_id>/update", methods=["POST"])
@login_required
@admin_required
def team_update(member_id):
    member = TeamMember.query.get_or_404(member_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name cannot be blank.", "danger")
        return redirect(url_for("admin.team"))
    member.name = name[:120]
    member.designation = request.form.get("designation", "").strip()[:120]
    member.credentials = request.form.get("credentials", "").strip()[:200]
    member.bio = request.form.get("bio", "").strip()
    member.sort_order = request.form.get("sort_order", type=int) or member.sort_order
    error = _save_member_photo(member, request.form.get("remove_photo"), request.files.get("photo"))
    if error:
        flash(error, "danger")
        return redirect(url_for("admin.team"))
    db.session.commit()
    flash("Team member updated.", "success")
    return redirect(url_for("admin.team"))


@admin_bp.route("/team/<int:member_id>/toggle", methods=["POST"])
@login_required
@admin_required
def team_toggle(member_id):
    member = TeamMember.query.get_or_404(member_id)
    member.active = not member.active
    db.session.commit()
    flash("Team updated.", "success")
    return redirect(url_for("admin.team"))


@admin_bp.route("/team/<int:member_id>/delete", methods=["POST"])
@login_required
@admin_required
def team_delete(member_id):
    member = TeamMember.query.get_or_404(member_id)
    db.session.delete(member)
    db.session.commit()
    flash("Team member removed.", "success")
    return redirect(url_for("admin.team"))


# ------------------------------------------------------------------ home video
@admin_bp.route("/home-video", methods=["GET", "POST"])
@login_required
@admin_required
def home_video():
    """Upload / replace the short intro video shown on the home page."""
    settings = Settings.get()
    if request.method == "POST":
        if request.form.get("remove_video"):
            delete_media(settings.home_video_filename)
            settings.home_video_filename = None
            settings.home_video_mimetype = None
            db.session.commit()
            flash("Home video removed.", "info")
            return redirect(url_for("admin.home_video"))

        settings.home_video_title = request.form.get("title", "").strip()[:150]
        settings.home_video_text = request.form.get("text", "").strip()[:300]

        filename, mimetype, error = save_home_video(request.files.get("video"))
        if error:
            flash(error, "danger")
            return redirect(url_for("admin.home_video"))
        if filename:
            delete_media(settings.home_video_filename)   # replace the old file
            settings.home_video_filename = filename
            settings.home_video_mimetype = mimetype

        poster, poster_type, perr = read_image_upload(request.files.get("poster"))
        if perr:
            flash(perr, "danger")
            return redirect(url_for("admin.home_video"))
        if poster is not None:
            settings.home_video_poster = poster
            settings.home_video_poster_mimetype = poster_type
        if request.form.get("remove_poster"):
            settings.home_video_poster = None
            settings.home_video_poster_mimetype = None

        db.session.commit()
        flash("Home video updated.", "success")
        return redirect(url_for("admin.home_video"))

    return render_template("admin/home_video.html", settings=settings)


# -------------------------------------------------- attendance summary report
def _compute_attendance_summary(args):
    """Student-wise and division-wise attendance over a date range."""
    today = date.today()
    start_raw, end_raw = args.get("start"), args.get("end")
    start = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else today.replace(day=1)
    end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else today
    class_id = args.get("class_id", type=int)
    division_id = args.get("division_id", type=int)

    students = limit_students(Student.query.filter_by(active=True))
    if class_id:
        students = students.filter_by(class_id=class_id)
    if division_id:
        students = students.filter_by(division_id=division_id)
    students = students.order_by(Student.name).all()
    student_ids = [s.id for s in students] or [-1]

    records = (
        Attendance.query
        .filter(Attendance.date >= start, Attendance.date <= end,
                Attendance.student_id.in_(student_ids))
        .all()
    )
    per_student = {}
    for r in records:
        d = per_student.setdefault(r.student_id, {"present": 0, "absent": 0})
        d[r.status] = d.get(r.status, 0) + 1

    student_rows = []
    for s in students:
        d = per_student.get(s.id, {"present": 0, "absent": 0})
        marked = d["present"] + d["absent"]
        pct = round(d["present"] / marked * 100, 1) if marked else 0.0
        student_rows.append({
            "student": s, "present": d["present"], "absent": d["absent"],
            "marked": marked, "pct": pct,
        })

    div_agg = {}
    for s in students:
        d = per_student.get(s.id, {"present": 0, "absent": 0})
        g = div_agg.setdefault(s.division_id, {"present": 0, "absent": 0, "students": 0})
        g["present"] += d["present"]; g["absent"] += d["absent"]; g["students"] += 1
    division_rows = []
    for div in visible_divisions(Division.query.all()):
        g = div_agg.get(div.id)
        if not g or g["students"] == 0:
            continue
        marked = g["present"] + g["absent"]
        pct = round(g["present"] / marked * 100, 1) if marked else 0.0
        division_rows.append({
            "division": div, "students": g["students"],
            "present": g["present"], "absent": g["absent"], "marked": marked, "pct": pct,
        })
    division_rows.sort(key=lambda r: (r["division"].school_class.sort_key, r["division"].name))

    label = f"{start.strftime('%d-%m-%Y')} to {end.strftime('%d-%m-%Y')}"
    return {
        "start": start, "end": end, "label": label,
        "student_rows": student_rows, "division_rows": division_rows,
        "class_id": class_id, "division_id": division_id,
    }


@admin_bp.route("/reports/attendance-summary")
@login_required
@office_required
def attendance_summary():
    data = _compute_attendance_summary(request.args)
    view = request.args.get("view", "student")
    return render_template(
        "admin/attendance_summary.html",
        classes=_classes_sorted(),
        selected_class_id=data["class_id"],
        selected_division_id=data["division_id"],
        view=view, **data,
    )


@admin_bp.route("/reports/attendance-summary/excel")
@login_required
@office_required
def attendance_summary_excel():
    data = _compute_attendance_summary(request.args)
    view = request.args.get("view", "student")
    if view == "division":
        headers = ["Class-Division", "Students", "Present", "Absent", "Marked", "Attendance %"]
        rows = [[f"{r['division'].school_class.name}-{r['division'].name}", r["students"],
                 r["present"], r["absent"], r["marked"], f"{r['pct']}%"] for r in data["division_rows"]]
    else:
        headers = ["Admission No", "Name", "Class-Division", "Present", "Absent", "Marked", "Attendance %"]
        rows = [[r["student"].admission_no, r["student"].name,
                 f"{r['student'].school_class.name}-{r['student'].division.name}",
                 r["present"], r["absent"], r["marked"], f"{r['pct']}%"] for r in data["student_rows"]]
    buffer = build_simple_sheet(f"Attendance {view}", headers, rows)
    return send_file(buffer, as_attachment=True,
                     download_name=f"attendance_{view}_{data['start']}_{data['end']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@admin_bp.route("/reports/attendance-summary/pdf")
@login_required
@office_required
def attendance_summary_pdf():
    data = _compute_attendance_summary(request.args)
    view = request.args.get("view", "student")
    if view == "division":
        columns = ["Class-Division", "Students", "Present", "Absent", "Marked", "Attendance %"]
        rows = [[f"{r['division'].school_class.name}-{r['division'].name}", r["students"],
                 r["present"], r["absent"], r["marked"], f"{r['pct']}%"] for r in data["division_rows"]]
    else:
        columns = ["Adm No", "Name", "Class-Div", "Present", "Absent", "Marked", "Attn %"]
        rows = [[r["student"].admission_no, r["student"].name,
                 f"{r['student'].school_class.name}-{r['student'].division.name}",
                 r["present"], r["absent"], r["marked"], f"{r['pct']}%"] for r in data["student_rows"]]
    return _pdf_response(
        "pdf/generic_report_pdf.html",
        f"attendance_{view}_{data['start']}_{data['end']}.pdf",
        title=f"Attendance Report ({'Division-wise' if view=='division' else 'Student-wise'})",
        meta=data["label"], columns=columns, rows=rows,
    )


# -------------------------------------------------- teacher payments
@admin_bp.route("/teacher-payments", methods=["GET", "POST"])
@login_required
@office_required
def teacher_payments():
    """Record cash paid to teachers (admin and office). Each is an expense."""
    if request.method == "POST":
        teacher_id = request.form.get("teacher_id", type=int)
        amount = request.form.get("amount", type=float)
        teacher = Teacher.query.get(teacher_id) if teacher_id else None
        if teacher is None:
            flash("Choose a teacher.", "danger")
        elif not amount or amount <= 0:
            flash("Enter an amount greater than zero.", "danger")
        else:
            pay_date_raw = request.form.get("pay_date")
            pay_date = datetime.strptime(pay_date_raw, "%Y-%m-%d").date() if pay_date_raw else date.today()
            db.session.add(TeacherPayment(
                teacher_id=teacher.id, amount=amount, pay_date=pay_date,
                purpose=request.form.get("purpose", "").strip()[:200],
                period=request.form.get("period", "").strip()[:40],
                mode=request.form.get("mode", "Cash").strip()[:20] or "Cash",
                paid_by=current_user.name,
                academic_year_id=active_academic_year_id(),
            ))
            db.session.commit()
            flash(f"Recorded Rs. {amount:,.0f} paid to {teacher.name}.", "success")
        return redirect(url_for("admin.teacher_payments", teacher_id=teacher_id))

    teacher_id = request.args.get("teacher_id", type=int)
    query = TeacherPayment.query
    if teacher_id:
        query = query.filter_by(teacher_id=teacher_id)
    payments = query.order_by(TeacherPayment.pay_date.desc(), TeacherPayment.created_at.desc()).limit(300).all()
    total = sum(p.amount for p in payments)
    return render_template(
        "admin/teacher_payments.html",
        teachers=Teacher.query.filter_by(active=True).order_by(Teacher.name).all(),
        payments=payments, total=total, selected_teacher_id=teacher_id,
    )


@admin_bp.route("/teacher-payments/<int:payment_id>/delete", methods=["POST"])
@login_required
@admin_required
def teacher_payment_delete(payment_id):
    payment = TeacherPayment.query.get_or_404(payment_id)
    db.session.delete(payment)
    db.session.commit()
    flash("Payment record deleted.", "info")
    return redirect(url_for("admin.teacher_payments"))


@admin_bp.route("/teacher-payments/excel")
@login_required
@office_required
def teacher_payments_excel():
    teacher_id = request.args.get("teacher_id", type=int)
    query = TeacherPayment.query
    if teacher_id:
        query = query.filter_by(teacher_id=teacher_id)
    payments = query.order_by(TeacherPayment.pay_date.desc()).all()
    headers = ["Date", "Teacher", "Amount", "Purpose", "Period", "Mode", "Paid By"]
    rows = [[p.pay_date.strftime("%d-%m-%Y"), p.teacher.name, f"{p.amount:.0f}",
             p.purpose or "", p.period or "", p.mode, p.paid_by or ""] for p in payments]
    totals = ["", "Total", f"{sum(p.amount for p in payments):.0f}", "", "", "", ""]
    buffer = build_simple_sheet("Teacher Payments", headers, rows, totals)
    return send_file(buffer, as_attachment=True, download_name="teacher_payments.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# -------------------------------------------------- expenses & other income
EXPENSE_CATEGORIES = ["Rent", "Electricity", "Water", "Internet & Phone", "Salaries",
                      "Supplies & Stationery", "Maintenance", "Marketing", "Travel",
                      "Events", "Refreshments", "Miscellaneous"]
INCOME_SOURCES = ["Donation", "Book / Material sales", "Rent received", "Event income",
                  "Registration fee", "Miscellaneous"]


def _parse_date(raw):
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else date.today()


@admin_bp.route("/expenses", methods=["GET", "POST"])
@login_required
@office_required
def expenses():
    if request.method == "POST":
        amount = request.form.get("amount", type=float)
        category = request.form.get("category", "").strip()[:60]
        if not category:
            flash("Choose or type a category.", "danger")
        elif not amount or amount <= 0:
            flash("Enter an amount greater than zero.", "danger")
        else:
            db.session.add(ExpenseEntry(
                category=category, amount=amount,
                expense_date=_parse_date(request.form.get("expense_date")),
                description=request.form.get("description", "").strip()[:200],
                payee=request.form.get("payee", "").strip()[:120],
                mode=request.form.get("mode", "Cash").strip()[:20] or "Cash",
                recorded_by=current_user.name,
                academic_year_id=active_academic_year_id(),
            ))
            db.session.commit()
            flash(f"Expense of Rs. {amount:,.0f} recorded.", "success")
        return redirect(url_for("admin.expenses"))

    entries = ExpenseEntry.query.order_by(ExpenseEntry.expense_date.desc(),
                                          ExpenseEntry.created_at.desc()).limit(300).all()
    return render_template("admin/expenses.html", entries=entries,
                           categories=EXPENSE_CATEGORIES, total=sum(e.amount for e in entries))


@admin_bp.route("/expenses/<int:entry_id>/delete", methods=["POST"])
@login_required
@admin_required
def expense_delete(entry_id):
    entry = ExpenseEntry.query.get_or_404(entry_id)
    db.session.delete(entry); db.session.commit()
    flash("Expense deleted.", "info")
    return redirect(url_for("admin.expenses"))


@admin_bp.route("/expenses/excel")
@login_required
@office_required
def expenses_excel():
    entries = ExpenseEntry.query.order_by(ExpenseEntry.expense_date.desc()).all()
    headers = ["Date", "Category", "Amount", "Description", "Payee", "Mode", "Recorded By"]
    rows = [[e.expense_date.strftime("%d-%m-%Y"), e.category, f"{e.amount:.0f}",
             e.description or "", e.payee or "", e.mode, e.recorded_by or ""] for e in entries]
    totals = ["", "Total", f"{sum(e.amount for e in entries):.0f}", "", "", "", ""]
    buffer = build_simple_sheet("Expenses", headers, rows, totals)
    return send_file(buffer, as_attachment=True, download_name="expenses.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@admin_bp.route("/other-income", methods=["GET", "POST"])
@login_required
@office_required
def other_income():
    if request.method == "POST":
        amount = request.form.get("amount", type=float)
        source = request.form.get("source", "").strip()[:60]
        if not source:
            flash("Choose or type a source.", "danger")
        elif not amount or amount <= 0:
            flash("Enter an amount greater than zero.", "danger")
        else:
            db.session.add(IncomeEntry(
                source=source, amount=amount,
                income_date=_parse_date(request.form.get("income_date")),
                description=request.form.get("description", "").strip()[:200],
                received_from=request.form.get("received_from", "").strip()[:120],
                mode=request.form.get("mode", "Cash").strip()[:20] or "Cash",
                recorded_by=current_user.name,
                academic_year_id=active_academic_year_id(),
            ))
            db.session.commit()
            flash(f"Income of Rs. {amount:,.0f} recorded.", "success")
        return redirect(url_for("admin.other_income"))

    entries = IncomeEntry.query.order_by(IncomeEntry.income_date.desc(),
                                         IncomeEntry.created_at.desc()).limit(300).all()
    return render_template("admin/other_income.html", entries=entries,
                           sources=INCOME_SOURCES, total=sum(e.amount for e in entries))


@admin_bp.route("/other-income/<int:entry_id>/delete", methods=["POST"])
@login_required
@admin_required
def income_delete(entry_id):
    entry = IncomeEntry.query.get_or_404(entry_id)
    db.session.delete(entry); db.session.commit()
    flash("Income entry deleted.", "info")
    return redirect(url_for("admin.other_income"))


@admin_bp.route("/other-income/excel")
@login_required
@office_required
def other_income_excel():
    entries = IncomeEntry.query.order_by(IncomeEntry.income_date.desc()).all()
    headers = ["Date", "Source", "Amount", "Description", "Received From", "Mode", "Recorded By"]
    rows = [[e.income_date.strftime("%d-%m-%Y"), e.source, f"{e.amount:.0f}",
             e.description or "", e.received_from or "", e.mode, e.recorded_by or ""] for e in entries]
    totals = ["", "Total", f"{sum(e.amount for e in entries):.0f}", "", "", "", ""]
    buffer = build_simple_sheet("Other Income", headers, rows, totals)
    return send_file(buffer, as_attachment=True, download_name="other_income.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _compute_accounts(args):
    """Income vs expenditure over a period, pulling from every source."""
    today = date.today()
    start = _parse_date(args.get("start")) if args.get("start") else today.replace(day=1)
    end = _parse_date(args.get("end")) if args.get("end") else today

    yid = active_academic_year_id()
    fee_q = db.session.query(func.coalesce(func.sum(FeePayment.amount), 0)).filter(
        FeePayment.payment_date >= start, FeePayment.payment_date <= end)
    inc_q = IncomeEntry.query.filter(IncomeEntry.income_date >= start, IncomeEntry.income_date <= end)
    tp_q = db.session.query(func.coalesce(func.sum(TeacherPayment.amount), 0)).filter(
        TeacherPayment.pay_date >= start, TeacherPayment.pay_date <= end)
    exp_q = ExpenseEntry.query.filter(ExpenseEntry.expense_date >= start, ExpenseEntry.expense_date <= end)
    if yid:
        fee_q = fee_q.filter(db.or_(FeePayment.academic_year_id == yid, FeePayment.academic_year_id.is_(None)))
        inc_q = inc_q.filter(db.or_(IncomeEntry.academic_year_id == yid, IncomeEntry.academic_year_id.is_(None)))
        tp_q = tp_q.filter(db.or_(TeacherPayment.academic_year_id == yid, TeacherPayment.academic_year_id.is_(None)))
        exp_q = exp_q.filter(db.or_(ExpenseEntry.academic_year_id == yid, ExpenseEntry.academic_year_id.is_(None)))
    fees = fee_q.scalar() or 0
    other_income_rows = inc_q.all()
    teacher_pay = tp_q.scalar() or 0
    expense_rows = exp_q.all()

    income_by = {"Student fees": fees}
    for r in other_income_rows:
        income_by[r.source] = income_by.get(r.source, 0) + r.amount
    expense_by = {"Teacher payments": teacher_pay}
    for r in expense_rows:
        expense_by[r.category] = expense_by.get(r.category, 0) + r.amount

    total_income = sum(income_by.values())
    total_expense = sum(expense_by.values())
    return {
        "start": start, "end": end,
        "label": f"{start.strftime('%d-%m-%Y')} to {end.strftime('%d-%m-%Y')}",
        "income_by": {k: v for k, v in income_by.items() if v},
        "expense_by": {k: v for k, v in expense_by.items() if v},
        "total_income": total_income, "total_expense": total_expense,
        "net": round(total_income - total_expense, 2),
    }


@admin_bp.route("/accounts")
@login_required
@admin_required
def accounts_dashboard():
    return render_template("admin/accounts_dashboard.html", **_compute_accounts(request.args))


@admin_bp.route("/accounts/excel")
@login_required
@admin_required
def accounts_excel():
    d = _compute_accounts(request.args)
    headers = ["Type", "Category", "Amount"]
    rows = [["Income", k, f"{v:.0f}"] for k, v in d["income_by"].items()]
    rows += [["Expense", k, f"{v:.0f}"] for k, v in d["expense_by"].items()]
    rows.append(["", "Total income", f"{d['total_income']:.0f}"])
    rows.append(["", "Total expense", f"{d['total_expense']:.0f}"])
    rows.append(["", "Net", f"{d['net']:.0f}"])
    buffer = build_simple_sheet("Accounts", headers, rows)
    return send_file(buffer, as_attachment=True,
                     download_name=f"accounts_{d['start']}_{d['end']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# -------------------------------------------------- payroll
def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _compute_payroll(args):
    today = date.today()
    year = args.get("year", type=int) or today.year
    month = args.get("month", type=int) or today.month
    start, end = _month_bounds(year, month)
    teachers = Teacher.query.filter_by(active=True).order_by(Teacher.name).all()
    rows = []
    for t in teachers:
        paid = (db.session.query(func.coalesce(func.sum(TeacherPayment.amount), 0))
                .filter(TeacherPayment.teacher_id == t.id,
                        TeacherPayment.pay_date >= start, TeacherPayment.pay_date <= end)
                .scalar() or 0)
        rows.append({"teacher": t, "salary": t.monthly_salary or 0, "paid": paid,
                     "balance": round((t.monthly_salary or 0) - paid, 2)})
    return {
        "rows": rows, "year": year, "month": month, "start": start, "end": end,
        "label": start.strftime("%B %Y"),
        "total_salary": sum(r["salary"] for r in rows),
        "total_paid": sum(r["paid"] for r in rows),
        "total_balance": sum(r["balance"] for r in rows),
    }


@admin_bp.route("/payroll")
@login_required
@admin_required
def payroll():
    data = _compute_payroll(request.args)
    years = list(range(date.today().year, date.today().year - 4, -1))
    return render_template("admin/payroll.html", years=years, months=list(enumerate(
        ["January","February","March","April","May","June","July","August",
         "September","October","November","December"], start=1)), **data)


@admin_bp.route("/payroll/salary/<int:teacher_id>", methods=["POST"])
@login_required
@admin_required
def payroll_set_salary(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    teacher.monthly_salary = request.form.get("monthly_salary", type=float) or 0
    db.session.commit()
    flash(f"Salary updated for {teacher.name}.", "success")
    return redirect(url_for("admin.payroll", year=request.form.get("year"),
                            month=request.form.get("month")))


@admin_bp.route("/payroll/excel")
@login_required
@admin_required
def payroll_excel():
    d = _compute_payroll(request.args)
    headers = ["Teacher", "Monthly Salary", "Paid", "Balance"]
    rows = [[r["teacher"].name, f"{r['salary']:.0f}", f"{r['paid']:.0f}", f"{r['balance']:.0f}"]
            for r in d["rows"]]
    totals = ["Total", f"{d['total_salary']:.0f}", f"{d['total_paid']:.0f}", f"{d['total_balance']:.0f}"]
    buffer = build_simple_sheet(f"Payroll {d['label']}", headers, rows, totals)
    return send_file(buffer, as_attachment=True,
                     download_name=f"payroll_{d['year']}_{d['month']:02d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@admin_bp.route("/payroll/payslip/<int:teacher_id>")
@login_required
@admin_required
def payroll_payslip(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    today = date.today()
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month
    start, end = _month_bounds(year, month)
    payments = (TeacherPayment.query
                .filter(TeacherPayment.teacher_id == teacher.id,
                        TeacherPayment.pay_date >= start, TeacherPayment.pay_date <= end)
                .order_by(TeacherPayment.pay_date).all())
    paid = sum(p.amount for p in payments)
    columns = ["Date", "Purpose", "Mode", "Amount"]
    rows = [[p.pay_date.strftime("%d-%m-%Y"), p.purpose or "-", p.mode, f"Rs. {p.amount:,.0f}"]
            for p in payments]
    return _pdf_response(
        "pdf/generic_report_pdf.html",
        f"payslip_{teacher.name.replace(' ', '_')}_{year}_{month:02d}.pdf",
        title=f"Payslip - {teacher.name}",
        meta=start.strftime("%B %Y"),
        columns=columns, rows=rows,
        totals_row=["", "", "Total paid", f"Rs. {paid:,.0f}"],
        summary_lines=[("Monthly salary", f"Rs. {teacher.monthly_salary or 0:,.0f}"),
                       ("Paid this month", f"Rs. {paid:,.0f}"),
                       ("Balance", f"Rs. {(teacher.monthly_salary or 0) - paid:,.0f}")],
    )


# -------------------------------------------------- timetable
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PERIODS = list(range(1, 7))


def _division_grid(division_id):
    """{(day, period): slot} for one division."""
    slots = TimetableSlot.query.filter_by(division_id=division_id).all()
    return {(sl.day, sl.period): sl for sl in slots}


@admin_bp.route("/timetable", methods=["GET", "POST"])
@login_required
@office_required
def timetable():
    division_id = request.values.get("division_id", type=int)
    division = Division.query.get(division_id) if division_id else None
    if division is not None:
        ensure_division_visible(division)

    if request.method == "POST" and division is not None:
        existing = _division_grid(division.id)
        conflicts = []
        for day in range(len(DAY_NAMES)):
            for period in PERIODS:
                subj = request.form.get(f"s_{day}_{period}", type=int)
                teacher_id = request.form.get(f"t_{day}_{period}", type=int)
                slot = existing.get((day, period))
                if not subj and not teacher_id:
                    if slot:
                        db.session.delete(slot)
                    continue
                if slot is None:
                    slot = TimetableSlot(division_id=division.id, day=day, period=period)
                    db.session.add(slot)
                slot.subject_id = subj or None
                slot.teacher_id = teacher_id or None
                # Warn if this teacher is already booked elsewhere at this time.
                if teacher_id:
                    clash = (TimetableSlot.query
                             .filter(TimetableSlot.teacher_id == teacher_id,
                                     TimetableSlot.day == day, TimetableSlot.period == period,
                                     TimetableSlot.division_id != division.id).first())
                    if clash:
                        conflicts.append(
                            f"{DAY_NAMES[day]} P{period}: teacher also in "
                            f"{clash.division.school_class.name}-{clash.division.name}")
        db.session.commit()
        if conflicts:
            flash("Saved, but note these clashes - " + "; ".join(conflicts[:6]), "warning")
        else:
            flash("Timetable saved.", "success")
        return redirect(url_for("admin.timetable", division_id=division.id))

    return render_template(
        "admin/timetable.html",
        classes=_classes_sorted(),
        divisions=visible_divisions(Division.query.all()),
        division=division,
        grid=_division_grid(division.id) if division else {},
        days=list(enumerate(DAY_NAMES)), periods=PERIODS,
        subjects=_subjects_sorted(),
        teachers=Teacher.query.filter_by(active=True).order_by(Teacher.name).all(),
    )


@admin_bp.route("/timetable/teacher/<int:teacher_id>")
@login_required
@office_required
def timetable_teacher(teacher_id):
    teacher = Teacher.query.get_or_404(teacher_id)
    slots = TimetableSlot.query.filter_by(teacher_id=teacher.id).all()
    grid = {}
    for sl in slots:
        grid.setdefault((sl.day, sl.period), []).append(sl)
    return render_template(
        "admin/timetable_teacher.html",
        teacher=teacher, grid=grid,
        days=list(enumerate(DAY_NAMES)), periods=PERIODS,
        teachers=Teacher.query.filter_by(active=True).order_by(Teacher.name).all(),
    )


# -------------------------------------------------- academic year
@admin_bp.route("/set-academic-year", methods=["POST"])
@login_required
@office_required
def set_academic_year():
    from flask import session
    year_id = request.form.get("academic_year_id", type=int)
    if year_id and AcademicYear.query.get(year_id):
        session["academic_year_id"] = year_id
    return redirect(request.referrer or url_for("admin.dashboard"))


@admin_bp.route("/academic-years", methods=["GET", "POST"])
@login_required
@admin_required
def academic_years():
    if request.method == "POST":
        label = request.form.get("label", "").strip()[:20]
        if not label:
            flash("Enter the year, e.g. 2027-2028.", "danger")
        elif AcademicYear.query.filter_by(label=label).first():
            flash("That year already exists.", "danger")
        else:
            make_current = bool(request.form.get("is_current"))
            if make_current:
                AcademicYear.query.update({AcademicYear.is_current: False})
            db.session.add(AcademicYear(label=label, is_current=make_current))
            db.session.commit()
            flash(f"Academic year {label} added.", "success")
        return redirect(url_for("admin.academic_years"))
    years = AcademicYear.query.order_by(AcademicYear.label.desc()).all()
    return render_template("admin/academic_years.html", years=years)


@admin_bp.route("/academic-years/<int:year_id>/set-current", methods=["POST"])
@login_required
@admin_required
def academic_year_set_current(year_id):
    year = AcademicYear.query.get_or_404(year_id)
    AcademicYear.query.update({AcademicYear.is_current: False})
    year.is_current = True
    db.session.commit()
    flash(f"{year.label} is now the current academic year.", "success")
    return redirect(url_for("admin.academic_years"))
