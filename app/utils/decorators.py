from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def office_required(view):
    """Admin, or the front-office coordinator.

    Guards the day-to-day desk work - admissions, fee collection, attendance,
    messages. Anything that changes what a family owes at the source (class
    fees), erases money (deleting a payment), or hands out access (Settings with
    its UPI ID, teacher accounts, password resets) keeps @admin_required.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ("admin", "office"):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "teacher":
            abort(403)
        return view(*args, **kwargs)

    return wrapped
