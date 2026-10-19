"""The working academic year.

Staff pick a year from a selector; the choice is kept in the session. New
transactions (fees, exams, accounts) are stamped with the active year, and the
year reports filter by it. Falls back to the year marked current.
"""
from flask import session


def active_academic_year():
    from ..models import AcademicYear

    year_id = session.get("academic_year_id")
    if year_id:
        year = AcademicYear.query.get(year_id)
        if year is not None:
            return year
    return AcademicYear.current()


def active_academic_year_id():
    year = active_academic_year()
    return year.id if year else None
