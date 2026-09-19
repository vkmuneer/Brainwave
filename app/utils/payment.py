"""UPI/GPay payment link helpers for fee reminders.

A parent-facing WhatsApp message includes a normal https:// link (so it is
reliably tappable inside WhatsApp) to our own /pay/<token> page. That page
then offers a upi://pay deep link pre-filled with the administrator's UPI ID
(GPay, PhonePe, etc. all register as handlers for this scheme on Android),
the amount due and a note - the parent just taps "Pay via GPay/UPI".
"""
import urllib.parse

from flask import current_app, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

TOKEN_SALT = "brainwave-pay-link"
TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 60  # 60 days


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def generate_pay_token(student_id: int) -> str:
    return _serializer().dumps(student_id, salt=TOKEN_SALT)


def verify_pay_token(token: str):
    """Returns the student_id, or None if the token is invalid/expired."""
    try:
        return _serializer().loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def build_pay_url(student_id: int) -> str:
    token = generate_pay_token(student_id)
    return url_for("public.pay", token=token, _external=True)


def build_upi_link(upi_id: str, payee_name: str, amount: float, note: str) -> str:
    params = {
        "pa": upi_id,
        "pn": payee_name or "Brainwave Academy",
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note,
    }
    return "upi://pay?" + urllib.parse.urlencode(params)


def monthly_fee_statement(student, pay_url, as_of, academy_name="Brainwave Academy"):
    """The month-end statement parents get, named for the month it covers.

    Spelled out line by line rather than as a single "you owe X", because the
    usual reply to a bare figure is a phone call asking how it was arrived at.
    Where the class has an installment plan, the headline figure is what the
    plan asks for by this month end - not the whole year's fee, which would
    read as though it were all overdue.
    """
    school_class = student.school_class
    month_name = as_of.strftime("%B %Y")
    due_by_now = school_class.scheduled_due(as_of, student.total_fee)
    shortfall = round(due_by_now - student.total_paid, 2)

    lines = [
        f"{academy_name}",
        f"Fee reminder for {month_name}",
        "",
        f"Student: {student.name}",
        f"Class: {school_class.name}-{student.division.name}",
        "",
        f"Total course fee: Rs. {student.class_fee:,.0f}",
    ]
    if student.discount_amount:
        lines.append(f"Discount allowed: Rs. {student.discount_amount:,.0f}")
        lines.append(f"Payable: Rs. {student.total_fee:,.0f}")
    lines.append(f"Paid so far: Rs. {student.total_paid:,.0f}")
    lines.append("")

    if school_class.has_schedule and due_by_now < student.total_fee:
        lines.append(
            f"Payable by {as_of.strftime('%d %B %Y')}: Rs. {due_by_now:,.0f}"
        )
        if shortfall > 0:
            lines.append(f"To be paid now: Rs. {shortfall:,.0f}")
        else:
            lines.append("Your payments are up to date for this month. Thank you.")
        lines.append(f"Remaining after that: Rs. {student.pending_fee - max(shortfall, 0):,.0f}")
    else:
        lines.append(f"Balance due: Rs. {student.pending_fee:,.0f}")

    lines += [
        "",
        f"Pay now: {pay_url}",
        "",
        "Please ignore this message if you have already paid. Thank you.",
    ]
    return "\n".join(lines)


def fee_reminder_message(student, pay_url):
    pending = student.pending_fee
    return (
        f"Dear Parent, this is a reminder from Brainwave Academy that a fee of "
        f"₹{pending:,.0f} is pending for {student.name} "
        f"(Class {student.school_class.name}-{student.division.name}). "
        f"Please pay conveniently here: {pay_url} Thank you."
    )
