"""Branch scoping for office coordinators.

One coordinator runs the High School branch and another the Higher Secondary
branch; neither should see the other's students, fees, attendance or exams.

Everything funnels through `visible_class_ids()`, which returns None for an
account that may see everything (admin) and a list of class ids otherwise - so
a new report only has to ask this one question.
"""
from flask import abort
from flask_login import current_user

from ..extensions import db


def current_branch():
    """The branch this account runs, or None for academy-wide access."""
    if not getattr(current_user, "is_authenticated", False):
        return None
    if current_user.is_admin:
        return None
    return current_user.branch


def visible_class_ids():
    """Class ids this account may see, or None meaning 'no restriction'."""
    branch = current_branch()
    if branch is None:
        return None
    return [c.id for c in branch.classes]


def resolved_class_ids(branch_id=None):
    """Class ids to show, combining the account's own scope with an optional
    admin-chosen branch filter.

    Office staff are always held to their own branch; passing a branch_id only
    narrows further (and can never widen past their own). For an admin, whose
    own scope is unrestricted, branch_id picks one branch, or None means all.
    """
    own = visible_class_ids()
    if not branch_id:
        return own
    from ..models import Branch

    branch = Branch.query.get(branch_id)
    chosen = [c.id for c in branch.classes] if branch else []
    if own is None:
        return chosen
    return [cid for cid in own if cid in chosen]


def scope_payments(query, class_ids):
    """Filter a FeePayment query down to students in the given classes.

    Uses a student-id subquery rather than a join, so it composes with a query
    that may already join Student (e.g. a name search) without a second join.
    """
    from ..models import FeePayment, Student

    if class_ids is None:
        return query
    student_ids = db.session.query(Student.id).filter(Student.class_id.in_(class_ids or [-1]))
    return query.filter(FeePayment.student_id.in_(student_ids))


def limit_students(query):
    from ..models import Student

    class_ids = visible_class_ids()
    if class_ids is None:
        return query
    return query.filter(Student.class_id.in_(class_ids or [-1]))


def visible_classes(classes):
    class_ids = visible_class_ids()
    if class_ids is None:
        return classes
    return [c for c in classes if c.id in class_ids]


def visible_divisions(divisions):
    class_ids = visible_class_ids()
    if class_ids is None:
        return divisions
    return [d for d in divisions if d.class_id in class_ids]


def ensure_class_visible(class_id):
    """Guard for a record reached by id - a coordinator who edits the URL must
    not reach the other branch's student, exam or division."""
    class_ids = visible_class_ids()
    if class_ids is not None and class_id not in class_ids:
        abort(403)


def ensure_student_visible(student):
    ensure_class_visible(student.class_id)
    return student


def ensure_division_visible(division):
    ensure_class_visible(division.class_id)
    return division
