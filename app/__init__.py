import os
from datetime import date

import click
from flask import Flask, render_template

from config import Config
from .extensions import db, csrf, login_manager


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    _check_secret_key(app)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from .utils.audit import register_audit_hooks

    register_audit_hooks(db)

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .auth.routes import auth_bp
    from .admin.routes import admin_bp
    from .teacher.routes import teacher_bp
    from .public.routes import public_bp
    from .portal.routes import portal_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(portal_bp)

    from flask import redirect, url_for
    from flask_login import current_user

    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.is_admin:
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("teacher.dashboard"))

    def _error_page(heading, message, icon, code):
        """A dead end should say who you are and offer a way back - Flask's
        default 403 just says the resource is 'read-protected', which tells a
        teacher or office coordinator nothing about what to do next."""
        return render_template("error.html", heading=heading, message=message, icon=icon), code

    @app.errorhandler(403)
    def _forbidden(_error):
        return _error_page(
            "Not available to your login",
            "Your account does not have access to this page. If you need something from "
            "it, ask the academy administrator.",
            "bi-lock",
            403,
        )

    @app.errorhandler(404)
    def _not_found(_error):
        return _error_page(
            "Page not found",
            "That link does not lead anywhere. It may have been removed, or the record "
            "may have been deleted.",
            "bi-question-circle",
            404,
        )

    @app.errorhandler(500)
    def _server_error(_error):
        return _error_page(
            "Something went wrong",
            "The page could not be loaded. Please try again - if it keeps happening, "
            "note what you were doing and tell the administrator.",
            "bi-exclamation-triangle",
            500,
        )

    @app.context_processor
    def inject_globals():
        pending_resets = 0
        pending_videos = 0
        if current_user.is_authenticated and current_user.is_admin:
            from .models import PasswordResetRequest, VideoClass

            pending_resets = PasswordResetRequest.query.filter_by(status="pending").count()
            pending_videos = VideoClass.query.filter_by(approval="pending").count()
        return {
            "today": date.today(),
            "app_name": "Brainwave Academy",
            "pending_reset_count": pending_resets,
            "pending_video_count": pending_videos,
        }

    with app.app_context():
        db.create_all()
        _auto_migrate(app)
        _migrate_attendance_sessions(app)
        _backfill_video_approval(app)
        _seed_branches(app)
        _backfill_masters(app)
        _ensure_seed_data(app)

    register_cli(app)

    return app


def _check_secret_key(app):
    """Refuse to serve real traffic with a SECRET_KEY that is published in the
    repo - session cookies are signed with it, so a known key lets anyone forge
    a login as the administrator. Allowed on localhost so development still
    works out of the box."""
    from config import PLACEHOLDER_SECRET_KEYS

    if app.config["SECRET_KEY"] not in PLACEHOLDER_SECRET_KEYS:
        return

    if os.environ.get("FLASK_DEBUG") == "1" or os.environ.get("ALLOW_DEFAULT_SECRET_KEY") == "1":
        app.logger.warning("[security] SECRET_KEY is a published placeholder - development only.")
        return

    raise RuntimeError(
        "SECRET_KEY is still the example value from .env.example, which is public.\n"
        "Set a real one before serving this to anyone:\n"
        "    python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
        "then put it in .env (or your host's environment variables) as SECRET_KEY=...\n"
        "To run locally with the placeholder anyway, set ALLOW_DEFAULT_SECRET_KEY=1."
    )


def _auto_migrate(app):
    """Adds any model columns that are missing from already-existing tables.

    db.create_all() only creates tables that don't exist yet - it never alters
    ones that do, so a schema change (like adding Student.place) would crash
    an existing deployment's database on the next boot without this. This is
    a lightweight stand-in for a full migration tool, sufficient because our
    schema changes are additive-only (new nullable columns/tables)."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # a brand-new table - db.create_all() already built it
        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            col_type = column.type.compile(dialect=db.engine.dialect)
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
            app.logger.info("[auto-migrate] added column %s.%s", table.name, column.name)


def _migrate_attendance_sessions(app):
    """Widen attendance's unique key from (student, date) to include session.

    Marking twice a day needs two rows per student per date, which the original
    constraint forbids. SQLite cannot drop a constraint declared inside CREATE
    TABLE, so the table is rebuilt and its rows copied across as full-day
    records; other databases can simply swap the constraint.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "attendance" not in inspector.get_table_names():
        return

    constraints = inspector.get_unique_constraints("attendance")
    stale = [c for c in constraints if set(c["column_names"]) == {"student_id", "date"}]
    if not stale:
        return

    if db.engine.dialect.name != "sqlite":
        with db.engine.begin() as conn:
            for c in stale:
                conn.execute(text(f'ALTER TABLE attendance DROP CONSTRAINT "{c["name"]}"'))
            conn.execute(
                text(
                    "ALTER TABLE attendance ADD CONSTRAINT uq_attendance_student_date_session "
                    "UNIQUE (student_id, date, session)"
                )
            )
        app.logger.info("[migrate] attendance unique key now includes session")
        return

    with db.engine.begin() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM attendance")).scalar()
        conn.execute(text("ALTER TABLE attendance RENAME TO attendance_legacy"))

    db.metadata.tables["attendance"].create(db.engine)

    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attendance "
                "(id, student_id, division_id, date, session, status, marked_by, created_at) "
                "SELECT id, student_id, division_id, date, 'full', status, marked_by, created_at "
                "FROM attendance_legacy"
            )
        )
        after = conn.execute(text("SELECT COUNT(*) FROM attendance")).scalar()
        if after != before:
            raise RuntimeError(
                f"attendance migration copied {after} of {before} rows - left "
                "attendance_legacy in place, database not modified further"
            )
        conn.execute(text("DROP TABLE attendance_legacy"))

    app.logger.info("[migrate] rebuilt attendance with session in its key (%s rows)", before)


def _seed_branches(app):
    """Create the two branches and place existing classes in them.

    Only runs while every class is still unassigned, so an admin who later
    moves a class between branches does not have it put back on next boot.
    """
    from .models import Branch, SchoolClass

    if SchoolClass.query.filter(SchoolClass.branch_id.isnot(None)).first():
        return

    layout = {"High School": ["9", "SSLC"], "Higher Secondary": ["+1", "+2"]}
    for branch_name, class_names in layout.items():
        branch = Branch.query.filter_by(name=branch_name).first()
        if branch is None:
            branch = Branch(name=branch_name)
            db.session.add(branch)
            db.session.flush()
        for class_name in class_names:
            school_class = SchoolClass.query.filter_by(name=class_name).first()
            if school_class is not None and school_class.branch_id is None:
                school_class.branch_id = branch.id

    db.session.commit()
    app.logger.info("[migrate] seeded branches and assigned classes")


def _backfill_video_approval(app):
    """Approve videos that predate the approval step.

    They were already visible to students, so leaving them NULL (and therefore
    unapproved) would pull published class material offline on upgrade.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "video_classes" not in inspector.get_table_names():
        return
    if "approval" not in {c["name"] for c in inspector.get_columns("video_classes")}:
        return

    with db.engine.begin() as conn:
        updated = conn.execute(
            text("UPDATE video_classes SET approval = 'approved' WHERE approval IS NULL")
        ).rowcount
    if updated:
        app.logger.info("[migrate] approved %s pre-existing video(s)", updated)


def _backfill_masters(app):
    """One-time (idempotent) migration for the subject/place/school master
    lists: seeds Place/SchoolMaster from any values already on Student rows,
    and Subject from the legacy free-text teachers.subject column (which the
    model no longer declares), linking each teacher to its matching Subject
    so nothing is lost when upgrading a deployment that predates this."""
    from sqlalchemy import inspect, text
    from .models import Student, Teacher, Subject, Place, SchoolMaster

    for value, in db.session.query(Student.place).distinct():
        value = (value or "").strip()
        if value and not Place.query.filter_by(name=value).first():
            db.session.add(Place(name=value))

    for value, in db.session.query(Student.school_name).distinct():
        value = (value or "").strip()
        if value and not SchoolMaster.query.filter_by(name=value).first():
            db.session.add(SchoolMaster(name=value))

    db.session.commit()

    inspector = inspect(db.engine)
    teacher_columns = {c["name"] for c in inspector.get_columns("teachers")}
    if "subject" in teacher_columns:
        rows = db.session.execute(
            text("SELECT id, subject FROM teachers WHERE subject IS NOT NULL AND subject != ''")
        ).fetchall()
        for teacher_id, subject_name in rows:
            subject_name = (subject_name or "").strip()
            if not subject_name:
                continue
            subject = Subject.query.filter_by(name=subject_name).first()
            if subject is None:
                subject = Subject(name=subject_name)
                db.session.add(subject)
                db.session.flush()
            teacher = db.session.get(Teacher, teacher_id)
            if teacher and subject not in teacher.subjects:
                teacher.subjects.append(subject)
        db.session.commit()


def _ensure_seed_data(app):
    """Create default classes/divisions and an admin account the first time
    the app runs, so the school can log in immediately after installation."""
    from .models import SchoolClass, Division, User, Settings

    if SchoolClass.query.first() is None:
        for name, fee in app.config["DEFAULT_CLASS_FEES"].items():
            school_class = SchoolClass(name=name, base_fee=fee)
            school_class.divisions.append(Division(name="A"))
            db.session.add(school_class)
        db.session.commit()

    if User.query.filter_by(role="admin").first() is None:
        admin = User(
            username=app.config["DEFAULT_ADMIN_USERNAME"],
            name="Administrator",
            role="admin",
        )
        admin.set_password(app.config["DEFAULT_ADMIN_PASSWORD"])
        db.session.add(admin)
        db.session.commit()

    if Settings.query.get(1) is None:
        db.session.add(Settings(id=1, academy_name="Brainwave Academy"))
        db.session.commit()


def register_cli(app):
    @app.cli.command("seed-db")
    def seed_db():
        """Re-run the initial seed (safe to run multiple times)."""
        _ensure_seed_data(app)
        print("Seed data ensured.")

    @app.cli.command("list-users")
    def list_users():
        """Show every login account, for when a username has been forgotten."""
        from .models import User

        for user in User.query.order_by(User.role, User.username):
            state = "" if user.active else "  (deactivated)"
            click.echo(f"{user.username:20} {user.role:8} {user.name}{state}")

    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option()
    def reset_password(username, password):
        """Reset any account's password from the terminal.

        The only way back in when the admin password is lost, since there is no
        second admin to reset it and no email address on file to send a link to.
        Requires shell access to the machine, which is what makes it safe.
        """
        from .models import User

        user = User.query.filter_by(username=username).first()
        if user is None:
            raise click.ClickException(
                f"No account named '{username}'. Run 'flask --app run list-users' to see them."
            )
        if len(password) < 4:
            raise click.ClickException("Password must be at least 4 characters.")

        user.set_password(password)
        db.session.commit()
        click.echo(f"Password updated for '{user.username}' ({user.role}).")
