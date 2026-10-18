"""Expenses, other income, and the accounts dashboard."""
from datetime import date
from conftest import login


def test_office_records_expense_and_income(client, app, data):
    from app.models import ExpenseEntry, IncomeEntry
    login(client, "office1", "Office@123")
    client.post("/admin/expenses",
                data={"category": "Rent", "amount": "15000", "description": "Nov rent",
                      "payee": "Landlord", "mode": "Bank"}, follow_redirects=True)
    client.post("/admin/other-income",
                data={"source": "Donation", "amount": "5000", "received_from": "Alumnus"},
                follow_redirects=True)
    with app.app_context():
        assert ExpenseEntry.query.first().amount == 15000
        assert IncomeEntry.query.first().source == "Donation"


def test_zero_amount_refused(client, app, data):
    from app.models import ExpenseEntry
    login(client, "admin", "admin123")
    client.post("/admin/expenses", data={"category": "Rent", "amount": "0"}, follow_redirects=True)
    with app.app_context():
        assert ExpenseEntry.query.count() == 0


def test_accounts_dashboard_sums_all_sources(client, app, data):
    from app.extensions import db
    from app.models import Student, FeePayment, TeacherPayment, ExpenseEntry, IncomeEntry, Teacher
    with app.app_context():
        s = Student.query.filter_by(admission_no="S1").first()
        t = Teacher.query.first()
        db.session.add_all([
            FeePayment(student_id=s.id, amount=8000, payment_date=date.today(), mode="Cash"),
            IncomeEntry(source="Donation", amount=2000, income_date=date.today()),
            TeacherPayment(teacher_id=t.id, amount=5000, pay_date=date.today()),
            ExpenseEntry(category="Rent", amount=3000, expense_date=date.today()),
        ])
        db.session.commit()

    login(client, "admin", "admin123")
    body = client.get("/admin/accounts").get_data(as_text=True)
    # Income 8000 + 2000 = 10,000 ; Expense 5000 + 3000 = 8,000 ; Net = 2,000
    assert "10,000" in body
    assert "8,000" in body
    assert "2,000" in body
    assert "Student fees" in body and "Teacher payments" in body


def test_accounts_dashboard_is_admin_only(client, app, data):
    login(client, "office1", "Office@123")
    assert client.get("/admin/accounts").status_code == 403


def test_excel_downloads(client, app, data):
    login(client, "admin", "admin123")
    for url in ("/admin/expenses/excel", "/admin/other-income/excel", "/admin/accounts/excel"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "spreadsheet" in r.headers.get("Content-Type", ""), url


def test_only_admin_deletes(client, app, data):
    from app.models import ExpenseEntry
    login(client, "admin", "admin123")
    client.post("/admin/expenses", data={"category": "Rent", "amount": "100"}, follow_redirects=True)
    with app.app_context():
        eid = ExpenseEntry.query.first().id
    office = app.test_client(); login(office, "office1", "Office@123")
    assert office.post(f"/admin/expenses/{eid}/delete").status_code == 403
