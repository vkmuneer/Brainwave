import re
from datetime import date, datetime
from urllib.parse import urlparse, parse_qs

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db

CLASS_ORDER = ["9", "SSLC", "+1", "+2"]

teacher_divisions = db.Table(
    "teacher_divisions",
    db.Column("teacher_id", db.Integer, db.ForeignKey("teachers.id"), primary_key=True),
    db.Column("division_id", db.Integer, db.ForeignKey("divisions.id"), primary_key=True),
)

teacher_subjects = db.Table(
    "teacher_subjects",
    db.Column("teacher_id", db.Integer, db.ForeignKey("teachers.id"), primary_key=True),
    db.Column("subject_id", db.Integer, db.ForeignKey("subjects.id"), primary_key=True),
)


class Subject(db.Model):
    """Admin-managed master list of subjects teachers can be assigned to."""

    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    def __repr__(self):
        return f"<Subject {self.name}>"


class Place(db.Model):
    """Admin-managed master list of places/localities students are from."""

    __tablename__ = "places"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

    def __repr__(self):
        return f"<Place {self.name}>"


class SchoolMaster(db.Model):
    """Admin-managed master list of (regular) schools students study in."""

    __tablename__ = "school_masters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)

    def __repr__(self):
        return f"<SchoolMaster {self.name}>"


class Branch(db.Model):
    """A campus or section of the academy - e.g. High School and Higher
    Secondary - each run by its own office coordinator."""

    __tablename__ = "branches"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    classes = db.relationship("SchoolClass", backref="branch")

    def __repr__(self):
        return f"<Branch {self.name}>"


class SchoolClass(db.Model):
    __tablename__ = "school_classes"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), unique=True, nullable=False)
    base_fee = db.Column(db.Float, nullable=False, default=0)
    branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=True)

    # Installment plan: a larger first payment at the end of the starting month,
    # then a fixed step each month until the whole fee is covered.
    first_installment = db.Column(db.Float, default=0, nullable=False)
    monthly_installment = db.Column(db.Float, default=0, nullable=False)
    schedule_start_month = db.Column(db.Integer, default=5, nullable=False)  # 5 = May

    @property
    def has_schedule(self):
        return bool(self.first_installment or self.monthly_installment)

    def months_elapsed(self, on_date):
        """Whole months from the start month to `on_date`, wrapping the year -
        May is 0, December 7, the following January 8."""
        delta = on_date.month - self.schedule_start_month
        return delta if delta >= 0 else delta + 12

    def scheduled_due(self, on_date, cap):
        """How much should have been paid by the end of `on_date`'s month.

        Capped at `cap` (the student's own payable amount after any discount),
        so a discounted student simply finishes the plan earlier rather than
        being asked for more than they owe.
        """
        if not self.has_schedule:
            return cap
        steps = self.months_elapsed(on_date)
        due = self.first_installment + self.monthly_installment * steps
        return min(due, cap)

    def schedule_summary(self):
        if not self.has_schedule:
            return "No installment plan - the whole fee is due."
        start = date(2000, self.schedule_start_month, 1).strftime("%B")
        return (
            f"Rs. {self.first_installment:,.0f} by end of {start}, then "
            f"Rs. {self.monthly_installment:,.0f} a month until "
            f"Rs. {self.base_fee:,.0f} is covered."
        )

    divisions = db.relationship(
        "Division", backref="school_class", cascade="all, delete-orphan", order_by="Division.name"
    )
    students = db.relationship("Student", backref="school_class")

    @property
    def sort_key(self):
        return CLASS_ORDER.index(self.name) if self.name in CLASS_ORDER else 99

    def __repr__(self):
        return f"<SchoolClass {self.name}>"


class Division(db.Model):
    __tablename__ = "divisions"
    __table_args__ = (db.UniqueConstraint("name", "class_id", name="uq_division_class"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(10), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_classes.id"), nullable=False)

    students = db.relationship("Student", backref="division")

    @property
    def display_name(self):
        return f"{self.school_class.name} - {self.name}"

    def __repr__(self):
        return f"<Division {self.display_name}>"


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(10), nullable=False)  # admin / office / teacher
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

    # Which branch this account runs. NULL means every branch, which is what an
    # admin gets; an office coordinator is expected to have one.
    branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=True)
    branch = db.relationship("Branch")

    teacher = db.relationship("Teacher", backref="user", uselist=False, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.active

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_office(self):
        return self.role == "office"

    @property
    def is_office_staff(self):
        """Anyone who works the admin side - admin or front office. Drives what
        the menus offer; each route still states its own requirement."""
        return self.role in ("admin", "office")

    @property
    def role_label(self):
        return {"admin": "Administrator", "office": "Office Coordinator", "teacher": "Teacher"}.get(
            self.role, self.role
        )

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class PasswordResetRequest(db.Model):
    """A request, raised from the login page, for an admin to reset a password.

    The record grants no authority on its own - an admin still has to set the
    new password by hand - which is what makes it safe to create from a public,
    unauthenticated page.
    """

    __tablename__ = "password_reset_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.String(255))
    status = db.Column(db.String(10), default="pending", nullable=False)  # pending / done / dismissed
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    handled_at = db.Column(db.DateTime)
    handled_by = db.Column(db.String(120))

    user = db.relationship("User")

    def __repr__(self):
        return f"<PasswordResetRequest user={self.user_id} {self.status}>"


class Teacher(db.Model):
    __tablename__ = "teachers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    active = db.Column(db.Boolean, default=True, nullable=False)

    divisions = db.relationship("Division", secondary=teacher_divisions, backref="teachers")
    subjects = db.relationship("Subject", secondary=teacher_subjects, backref="teachers")

    @property
    def subject_names(self):
        return ", ".join(s.name for s in sorted(self.subjects, key=lambda s: s.name))

    def __repr__(self):
        return f"<Teacher {self.name}>"


class Student(db.Model):
    __tablename__ = "students"

    id = db.Column(db.Integer, primary_key=True)
    admission_no = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_classes.id"), nullable=False)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False)

    parent_name = db.Column(db.String(120))
    parent_whatsapp = db.Column(db.String(20), nullable=False)
    address = db.Column(db.String(255))
    place = db.Column(db.String(120))
    school_name = db.Column(db.String(150))

    dob = db.Column(db.Date, nullable=True)
    admission_date = db.Column(db.Date, default=date.today, nullable=False)

    discount_amount = db.Column(db.Float, default=0, nullable=False)
    discount_reason = db.Column(db.String(255))

    active = db.Column(db.Boolean, default=True, nullable=False)

    payments = db.relationship(
        "FeePayment", backref="student", cascade="all, delete-orphan", order_by="FeePayment.payment_date"
    )
    attendances = db.relationship("Attendance", backref="student", cascade="all, delete-orphan")

    @property
    def class_fee(self):
        """The fee fixed for this class. Not adjustable per student - a
        concession is recorded as a discount instead, so the reduction is
        visible and shows up in the discount report."""
        return self.school_class.base_fee

    @property
    def total_fee(self):
        return max(self.class_fee - (self.discount_amount or 0), 0)

    @property
    def total_paid(self):
        return sum(p.amount for p in self.payments)

    @property
    def pending_fee(self):
        return round(self.total_fee - self.total_paid, 2)

    @property
    def payment_status(self):
        if self.pending_fee <= 0:
            return "Paid"
        if self.total_paid > 0:
            return "Partial"
        return "Unpaid"

    def __repr__(self):
        return f"<Student {self.admission_no} {self.name}>"


class FeePayment(db.Model):
    __tablename__ = "fee_payments"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.Date, default=date.today, nullable=False)
    mode = db.Column(db.String(20), default="Cash", nullable=False)
    receipt_no = db.Column(db.String(30))
    remarks = db.Column(db.String(255))
    recorded_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FeePayment {self.student_id} {self.amount}>"


ATTENDANCE_SESSIONS = {
    "full": "Full day",
    "fn": "Forenoon",
    "an": "Afternoon",
}


class Attendance(db.Model):
    __tablename__ = "attendance"
    # Session is part of the key: an academy marking twice a day needs a
    # forenoon and an afternoon row for the same student on the same date.
    __table_args__ = (
        db.UniqueConstraint("student_id", "date", "session", name="uq_attendance_student_date_session"),
    )

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    session = db.Column(db.String(4), nullable=False, default="full")  # full / fn / an
    status = db.Column(db.String(10), nullable=False)  # 'present' / 'absent'
    marked_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def session_label(self):
        return ATTENDANCE_SESSIONS.get(self.session, self.session)

    def __repr__(self):
        return f"<Attendance {self.student_id} {self.date} {self.session} {self.status}>"


class MessageLog(db.Model):
    __tablename__ = "message_logs"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    date = db.Column(db.Date, default=date.today)
    category = db.Column(db.String(20), default="attendance", nullable=False)  # attendance / fee_reminder
    message = db.Column(db.Text, nullable=False)
    phone = db.Column(db.String(20))
    status = db.Column(db.String(20), default="pending")  # sent / failed / manual
    detail = db.Column(db.String(255))
    manual_link = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("Student")

    def __repr__(self):
        return f"<MessageLog {self.student_id} {self.status}>"


class Settings(db.Model):
    """Singleton row (id=1) holding academy-wide, admin-editable settings."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    academy_name = db.Column(db.String(120), default="Brainwave Academy", nullable=False)
    tagline = db.Column(db.String(150))
    address = db.Column(db.String(255))
    phone = db.Column(db.String(80))
    email = db.Column(db.String(120))

    # Held in the database rather than as a file on disk so that one backup of
    # brainwave.db captures the branding too, and so a redeploy onto a fresh
    # filesystem does not silently lose the logo.
    logo_data = db.Column(db.LargeBinary)
    logo_mimetype = db.Column(db.String(40))

    upi_id = db.Column(db.String(120))
    upi_payee_name = db.Column(db.String(120))

    # Off by default: the portal signs parents in with their own phone number as
    # both username and password, so it should only be reachable once the
    # academy has decided to switch it on.
    student_login_enabled = db.Column(db.Boolean, default=False, nullable=False)

    # 'single' = one register a day, 'twice' = separate forenoon and afternoon.
    attendance_sessions = db.Column(db.String(10), default="single", nullable=False)
    instant_absence_alert = db.Column(db.Boolean, default=True, nullable=False)

    @property
    def twice_daily_attendance(self):
        return self.attendance_sessions == "twice"

    @property
    def session_choices(self):
        return ["fn", "an"] if self.twice_daily_attendance else ["full"]

    @property
    def contact_line(self):
        """Phone and email joined for a single line under the address."""
        parts = []
        if self.phone:
            parts.append(f"Phone: {self.phone}")
        if self.email:
            parts.append(self.email)
        return "  |  ".join(parts)

    @classmethod
    def get(cls):
        settings = cls.query.get(1)
        if settings is None:
            settings = cls(id=1, academy_name="Brainwave Academy")
            db.session.add(settings)
            db.session.commit()
        return settings

    def __repr__(self):
        return f"<Settings {self.academy_name}>"


_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,80}$")


class AuditLog(db.Model):
    """Who changed what, and from what to what.

    Written automatically from a session hook rather than by each route, so a
    new edit screen is covered without anyone remembering to log it.
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    actor_name = db.Column(db.String(120))
    actor_role = db.Column(db.String(10))

    action = db.Column(db.String(10), nullable=False)  # created / updated / deleted
    entity_type = db.Column(db.String(40), nullable=False, index=True)
    entity_id = db.Column(db.Integer)
    entity_label = db.Column(db.String(160))
    summary = db.Column(db.Text)

    def __repr__(self):
        return f"<AuditLog {self.action} {self.entity_type}#{self.entity_id}>"


class VideoClass(db.Model):
    """A recorded class a teacher publishes to one class (or one division).

    Only the link is stored for now - hosting video files on shared hosting
    would exhaust the disk quota - but `file_path` is reserved so uploads can
    be added later without a second model.
    """

    __tablename__ = "video_classes"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(500))

    class_id = db.Column(db.Integer, db.ForeignKey("school_classes.id"), nullable=False)
    # NULL means "every division of this class".
    division_id = db.Column(db.Integer, db.ForeignKey("divisions.id"), nullable=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)

    url = db.Column(db.String(500))
    file_path = db.Column(db.String(300))

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    uploaded_by_name = db.Column(db.String(120))
    published = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Nothing reaches students until an admin has looked at it.
    approval = db.Column(db.String(10), default="pending", nullable=False)  # pending/approved/rejected
    reviewed_by = db.Column(db.String(120))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.String(255))

    @property
    def is_approved(self):
        return self.approval == "approved"

    @property
    def status_label(self):
        if self.approval == "rejected":
            return "Rejected"
        if self.approval != "approved":
            return "Waiting for approval"
        return "Visible to students" if self.published else "Approved, hidden by you"

    school_class = db.relationship("SchoolClass")
    division = db.relationship("Division")
    subject = db.relationship("Subject")

    @property
    def audience(self):
        if self.division:
            return self.division.display_name
        return f"{self.school_class.name} (all divisions)"

    @property
    def embed_url(self):
        """A player URL to put in an iframe, or None to fall back to a link.

        Built from an id extracted from a recognised host - never from the
        pasted URL directly, since anything dropped straight into an iframe src
        would be a way to render arbitrary content inside the portal.
        """
        if not self.url:
            return None

        parsed = urlparse(self.url)
        host = (parsed.netloc or "").lower()
        host = host[4:] if host.startswith("www.") else host
        video_id = ""

        if host == "youtu.be":
            video_id = parsed.path.lstrip("/").split("/")[0]
        elif host in ("youtube.com", "m.youtube.com", "youtube-nocookie.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/embed/", "/shorts/", "/live/")):
                parts = parsed.path.split("/")
                video_id = parts[2] if len(parts) > 2 else ""
        elif host == "drive.google.com":
            parts = parsed.path.split("/")
            if len(parts) > 4 and parts[1] == "file" and parts[2] == "d":
                if _DRIVE_ID.match(parts[3]):
                    return f"https://drive.google.com/file/d/{parts[3]}/preview"
            return None

        if _YOUTUBE_ID.match(video_id):
            # nocookie host so watching a class video does not leave ad-tracking
            # cookies on a student's phone.
            return f"https://www.youtube-nocookie.com/embed/{video_id}"
        return None

    def visible_to(self, student):
        if not self.published or not self.is_approved:
            return False
        if student.class_id != self.class_id:
            return False
        return self.division_id is None or self.division_id == student.division_id

    def __repr__(self):
        return f"<VideoClass {self.title}>"


GRADE_BANDS = [
    (90, "A+"),
    (80, "A"),
    (70, "B+"),
    (60, "B"),
    (50, "C"),
    (35, "D"),
    (0, "F"),
]


def grade_for_percentage(pct):
    for threshold, grade in GRADE_BANDS:
        if pct >= threshold:
            return grade
    return "F"


class Exam(db.Model):
    """An examination for a whole class (all its divisions), e.g. a term exam."""

    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("school_classes.id"), nullable=False)
    exam_date = db.Column(db.Date, default=date.today, nullable=False)
    description = db.Column(db.String(255))

    school_class = db.relationship("SchoolClass", backref="exams")
    exam_subjects = db.relationship(
        "ExamSubject", backref="exam", cascade="all, delete-orphan", order_by="ExamSubject.id"
    )

    @property
    def total_max_marks(self):
        return sum(es.max_marks for es in self.exam_subjects)

    def __repr__(self):
        return f"<Exam {self.name}>"


class ExamSubject(db.Model):
    """One subject within an exam, with its own max/pass marks."""

    __tablename__ = "exam_subjects"
    __table_args__ = (db.UniqueConstraint("exam_id", "subject_id", name="uq_exam_subject"),)

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    max_marks = db.Column(db.Float, nullable=False, default=100)
    pass_marks = db.Column(db.Float, nullable=False, default=35)

    subject = db.relationship("Subject")
    marks = db.relationship("ExamMark", backref="exam_subject", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ExamSubject exam={self.exam_id} subject={self.subject_id}>"


class ExamMark(db.Model):
    """A single student's marks in one subject of one exam."""

    __tablename__ = "exam_marks"
    __table_args__ = (db.UniqueConstraint("exam_subject_id", "student_id", name="uq_exam_subject_student"),)

    id = db.Column(db.Integer, primary_key=True)
    exam_subject_id = db.Column(db.Integer, db.ForeignKey("exam_subjects.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    marks_obtained = db.Column(db.Float, nullable=False)
    remarks = db.Column(db.String(255))
    entered_by = db.Column(db.String(120))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship("Student")

    @property
    def is_pass(self):
        return self.marks_obtained >= self.exam_subject.pass_marks

    @property
    def percentage(self):
        if not self.exam_subject.max_marks:
            return 0
        return round(self.marks_obtained / self.exam_subject.max_marks * 100, 1)

    def __repr__(self):
        return f"<ExamMark student={self.student_id} marks={self.marks_obtained}>"
