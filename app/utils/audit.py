"""Automatic audit logging.

Hooks the session rather than each route, so any screen that edits a tracked
record is covered - including ones added later by someone who never reads this
file.

Deliberately narrow: attendance marks, exam marks and message logs are high
volume and already carry their own marked_by/entered_by, so logging them would
bury the entries that matter (money, fees, accounts, settings) in noise.
"""
from flask import has_request_context
from flask_login import current_user
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import event

# model name -> (label for humans, callable giving a readable identifier)
TRACKED = {
    "Student": ("student", lambda o: f"{o.name} ({o.admission_no})"),
    "FeePayment": ("payment", lambda o: f"Rs. {o.amount:,.0f} - {o.receipt_no or 'no receipt'}"),
    "SchoolClass": ("class", lambda o: o.name),
    "Division": ("division", lambda o: o.name),
    "Settings": ("settings", lambda o: o.academy_name),
    "User": ("account", lambda o: f"{o.name} ({o.username})"),
    "Teacher": ("teacher", lambda o: o.name),
    "VideoClass": ("video", lambda o: o.title),
    "Exam": ("exam", lambda o: o.name),
}

# Never write these values into the log.
REDACTED = {"password_hash", "logo_data"}

# Noise: changed on every save without being a meaningful edit.
IGNORED = {"created_at", "updated_at", "reviewed_at"}


def _actor():
    if has_request_context() and getattr(current_user, "is_authenticated", False):
        return current_user.name, current_user.role
    return "system", "system"


def _describe(value):
    if value is None or value == "":
        return "(empty)"
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _changes(obj):
    """Field-level 'name: old -> new' lines for a modified object."""
    lines = []
    state = sa_inspect(obj)
    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in IGNORED:
            continue
        history = state.attrs[key].history
        if not history.has_changes():
            continue
        if key in REDACTED:
            lines.append(f"{key}: changed")
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        if old == new:
            continue
        lines.append(f"{key}: {_describe(old)} -> {_describe(new)}")
    return lines


def _snapshot(obj):
    """Every stored value of a record, so a deletion can be reconstructed.

    A label alone says a payment vanished; it does not say whose it was or when
    it was taken, which is exactly what someone investigating needs.
    """
    lines = []
    for attr in sa_inspect(obj).mapper.column_attrs:
        if attr.key in REDACTED:
            continue
        value = getattr(obj, attr.key, None)
        if value not in (None, ""):
            lines.append(f"{attr.key}: {_describe(value)}")
    return "\n".join(lines)


def _entry(AuditLog, obj, action, summary):
    name, role = _actor()
    entity_type, labeller = TRACKED[type(obj).__name__]
    try:
        label = labeller(obj)
    except Exception:  # noqa: BLE001 - a half-built object must not break the save
        label = None
    return AuditLog(
        actor_name=name,
        actor_role=role,
        action=action,
        entity_type=entity_type,
        entity_id=getattr(obj, "id", None),
        entity_label=label,
        summary=summary,
    )


def register_audit_hooks(db):
    from ..models import AuditLog

    @event.listens_for(db.session, "before_flush")
    def _log_changes(session, flush_context, instances):  # noqa: ANN001
        entries = []

        for obj in session.new:
            if type(obj).__name__ in TRACKED:
                entries.append(_entry(AuditLog, obj, "created", None))

        for obj in session.dirty:
            if type(obj).__name__ not in TRACKED or not session.is_modified(obj):
                continue
            lines = _changes(obj)
            if lines:
                entries.append(_entry(AuditLog, obj, "updated", "\n".join(lines)))

        for obj in session.deleted:
            if type(obj).__name__ in TRACKED:
                entries.append(_entry(AuditLog, obj, "deleted", _snapshot(obj)))

        for entry in entries:
            session.add(entry)
