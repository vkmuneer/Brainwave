"""Shared attendance marking, used by both the teacher and the office views.

Kept in one place so a register saved by a teacher and one saved by the office
coordinator behave identically - same session handling, same absence alerts.
"""
from ..extensions import db
from ..models import Attendance, MessageLog, Settings, ATTENDANCE_SESSIONS
from .whatsapp import send_whatsapp_message, absence_message


def resolve_session(raw, settings=None):
    """The session to mark, constrained to what the academy has switched on.

    An academy on single daily marking always gets 'full', so a stale ?session=fn
    link cannot create rows the rest of the app would not show.
    """
    settings = settings or Settings.get()
    allowed = settings.session_choices
    if raw in allowed:
        return raw
    return allowed[0]


def session_label(session):
    return ATTENDANCE_SESSIONS.get(session, session)


def existing_status_map(division_id, att_date, session):
    return {
        a.student_id: a.status
        for a in Attendance.query.filter_by(
            division_id=division_id, date=att_date, session=session
        ).all()
    }


def save_attendance(division, att_date, session, students, form, marked_by):
    """Writes one row per student for this division/date/session.

    Returns the students marked absent, so the caller can report the count.
    """
    existing = {
        a.student_id: a
        for a in Attendance.query.filter_by(
            division_id=division.id, date=att_date, session=session
        ).all()
    }
    absentees = []

    for student in students:
        is_present = form.get(f"present_{student.id}") == "on"
        status = "present" if is_present else "absent"
        record = existing.get(student.id)
        if record:
            record.status = status
            record.marked_by = marked_by
        else:
            db.session.add(
                Attendance(
                    student_id=student.id,
                    division_id=division.id,
                    date=att_date,
                    session=session,
                    status=status,
                    marked_by=marked_by,
                )
            )
        if not is_present:
            absentees.append(student)

    db.session.commit()
    return absentees


def send_absence_alerts(absentees, division, att_date, session, settings=None):
    """Immediate per-absence WhatsApp alerts, if the academy still wants them.

    Returns how many were queued - zero when the office has chosen to rely on
    the once-a-day summary instead, so parents are not messaged twice.
    """
    settings = settings or Settings.get()
    if not settings.instant_absence_alert or not absentees:
        return 0

    for student in absentees:
        message = absence_message(student, division.school_class.name, division.name, att_date)
        if session != "full":
            message = message.replace(
                "was ABSENT today", f"was ABSENT in the {session_label(session).lower()} session today"
            )
        result = send_whatsapp_message(student.parent_whatsapp, message)
        db.session.add(
            MessageLog(
                student_id=student.id,
                date=att_date,
                message=message,
                phone=student.parent_whatsapp,
                status=result["status"],
                detail=result["detail"],
                manual_link=result.get("link"),
            )
        )
    db.session.commit()
    return len(absentees)
