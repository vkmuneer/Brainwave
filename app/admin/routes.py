import calendar
from datetime import date, datetime, timedelta
from io import BytesIO

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
)
from ..utils.pdf import render_pdf
from ..utils.exam_analysis import compute_exam_results, build_report_context, student_progress
from ..utils.video import is_acceptable_link


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
    students = limit_students(Student.query.filter_by(active=True)).all()
    total_expected = sum(s.total_fee for s in students)
    total_collected = sum(s.total_paid for s in students)
    total_pending = round(total_expected - total_collected, 2)

    today = date.today()
    today_collection = (
        db.session.query(func.coalesce(func.sum(FeePayment.amount), 0))
        .filter(FeePayment.payment_date == today)
        .scalar()
    )
    today_absentees = Attendance.query.filter_by(date=today, status="absent").count()
    today_present = Attendance.query.filter_by(date=today, status="present").count()

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
        classes=_classes_sorted(),
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
@admin_bp.route("/students")
@login_required
@office_required
def students():
    class_id = request.args.get("class_id", type=int)
    division_id = request.args.get("division_id", type=int)
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")

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
    exam_id = request.args.get("exam_id", type=int)
    min_pct = request.args.get("min_pct", type=float)
    max_pct = request.args.get("max_pct", type=float)
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

    return render_template(
        "admin/students.html",
        students=student_list,
        classes=_classes_sorted(),
        selected_class_id=class_id,
        selected_division_id=division_id,
        q=q,
        status=status,
        exam_options=exam_options,
        exam=exam,
        exam_id=exam_id,
        min_pct=min_pct,
        max_pct=max_pct,
        scores=scores,
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

    if len(new_password) < 4:
        flash("Password must be at least 4 characters.", "danger")
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
    payments_query = FeePayment.query.filter(
        FeePayment.payment_date >= start, FeePayment.payment_date <= end
    )
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
    q = args.get("q", "").strip()

    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Student.name.ilike(like), Student.admission_no.ilike(like)))

    student_list = query.order_by(Student.name).all()
    totals = {
        "expected": sum(s.total_fee for s in student_list),
        "collected": sum(s.total_paid for s in student_list),
        "pending": sum(s.pending_fee for s in student_list),
    }
    return student_list, class_id, q, totals


@admin_bp.route("/reports/student-wise")
@login_required
@office_required
def student_wise_report():
    student_list, class_id, q, totals = _compute_student_wise_report(request.args)
    return render_template(
        "admin/student_wise_report.html",
        students=student_list,
        classes=_classes_sorted(),
        selected_class_id=class_id,
        q=q,
        totals=totals,
    )


@admin_bp.route("/reports/student-wise/pdf")
@login_required
@office_required
def student_wise_report_pdf():
    student_list, class_id, q, totals = _compute_student_wise_report(request.args)
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
    q = args.get("q", "").strip()
    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Student.name.ilike(like), Student.admission_no.ilike(like)))

    student_list = [s for s in query.all() if s.pending_fee > 0]
    student_list.sort(key=lambda s: s.pending_fee, reverse=True)
    total_pending = sum(s.pending_fee for s in student_list)
    return student_list, class_id, q, total_pending


@admin_bp.route("/reports/pending-fees")
@login_required
@office_required
def pending_fees_report():
    student_list, class_id, q, total_pending = _compute_pending_fees_report(request.args)
    return render_template(
        "admin/pending_fees.html",
        students=student_list,
        classes=_classes_sorted(),
        selected_class_id=class_id,
        q=q,
        total_pending=total_pending,
    )


@admin_bp.route("/reports/pending-fees/pdf")
@login_required
@office_required
def pending_fees_report_pdf():
    student_list, class_id, q, total_pending = _compute_pending_fees_report(request.args)
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
    query = limit_students(Student.query.filter_by(active=True))
    if class_id:
        query = query.filter_by(class_id=class_id)
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

        saved = 0
        total = 0.0
        errors = []

        for row in rows:
            admission_no = str(row.get("admission_no", "")).strip()
            student = students.get(admission_no.lower())
            if student is None:
                errors.append(f"Row {row['_row']}: no active student {admission_no}.")
                continue

            try:
                amount = float(row["amount"])
            except (TypeError, ValueError):
                errors.append(f"Row {row['_row']}: '{row['amount']}' is not an amount.")
                continue
            if amount <= 0:
                errors.append(f"Row {row['_row']}: amount must be more than zero.")
                continue
            # The single-payment form refuses overpayment; the sheet must too,
            # or a typo silently creates a credit nobody reconciles.
            if amount > student.pending_fee:
                errors.append(
                    f"Row {row['_row']}: {student.name} owes only "
                    f"Rs. {student.pending_fee:,.0f} - Rs. {amount:,.0f} rejected."
                )
                continue

            raw_date = row.get("payment_date")
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
                errors.append(f"Row {row['_row']}: '{raw_date}' is not a date (use YYYY-MM-DD).")
                continue

            mode = (str(row.get("mode") or "Cash")).strip() or "Cash"
            payment = FeePayment(
                student_id=student.id,
                amount=amount,
                payment_date=paid_on,
                mode=mode,
                remarks=(str(row.get("remarks") or "")).strip()[:255],
                recorded_by=current_user.name,
            )
            db.session.add(payment)
            db.session.flush()
            payment.receipt_no = f"BW{payment.id:05d}"
            saved += 1
            total += amount

        db.session.commit()

        if saved:
            flash(f"Recorded {saved} payment(s) totalling Rs. {total:,.0f}.", "success")
        for message in errors[:12]:
            flash(message, "warning")
        if len(errors) > 12:
            flash(f"...and {len(errors) - 12} more problem(s) not shown.", "warning")
        if not saved and not errors:
            flash("Nothing to record - no amounts were filled in.", "info")
        return redirect(url_for("admin.payments_bulk_upload"))

    return render_template("admin/payments_bulk_upload.html", classes=_classes_sorted())


@admin_bp.route("/payments/bulk-upload/template")
@login_required
@office_required
def payments_bulk_template():
    query = limit_students(Student.query.filter_by(active=True))
    class_id = request.args.get("class_id", type=int)
    if class_id:
        ensure_class_visible(class_id)
        query = query.filter_by(class_id=class_id)

    students = [s for s in query.order_by(Student.name).all() if s.pending_fee > 0]
    buffer = build_payments_template(students)
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

        if not username or not name or len(password) < 4:
            flash("Username, name and a password of at least 4 characters are required.", "danger")
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
    exam_list = Exam.query.order_by(Exam.exam_date.desc()).all()
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
        next_seq = max([v.sequence for v in course.videos], default=0) + 1
        db.session.add(
            CourseVideo(
                course_id=course.id,
                title=title,
                url=url,
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
