"""Every page an admin can reach should load - a template typo breaks a page
without touching anything else, and only opening it finds that."""
import pytest

from conftest import login

ADMIN_PAGES = [
    "/admin/dashboard", "/admin/students", "/admin/students/new",
    "/admin/students/bulk-upload", "/admin/attendance/mark",
    "/admin/reports/attendance", "/admin/reports/finance",
    "/admin/reports/pending-fees", "/admin/reports/student-wise",
    "/admin/reports/discounts", "/admin/reports/class-wise",
    "/admin/payments/bulk-upload", "/admin/exams", "/admin/exams/new",
    "/admin/messages", "/admin/broadcast", "/admin/feedback",
    "/admin/teachers", "/admin/teachers/new", "/admin/office-staff",
    "/admin/classes", "/admin/masters", "/admin/videos",
    "/admin/password-requests", "/admin/audit-log", "/admin/settings",
    "/admin/courses", "/admin/subscriptions",
]

PUBLIC_PAGES = ["/", "/home", "/learn/", "/learn/login", "/learn/register",
                "/login", "/forgot-password", "/portal/login"]


@pytest.mark.parametrize("url", ADMIN_PAGES)
def test_admin_page_loads(client, data, url):
    login(client, "admin", "admin123")
    response = client.get(url, follow_redirects=True)
    assert response.status_code == 200, url
    assert b"Something went wrong" not in response.data, url


@pytest.mark.parametrize("url", PUBLIC_PAGES)
def test_public_page_loads(client, data, url):
    response = client.get(url, follow_redirects=True)
    assert response.status_code == 200, url


def test_teacher_pages_load(client, data):
    login(client, "teach1", "Teach@123")
    for url in ["/teacher/dashboard", "/teacher/attendance", "/teacher/students",
                "/teacher/exams", "/teacher/videos", "/teacher/attendance/history"]:
        response = client.get(url, follow_redirects=True)
        assert response.status_code == 200, url


def test_portal_pages_load(client, data):
    from conftest import portal_login

    portal_login(client, "9000000001")
    for url in ["/portal/", "/portal/attendance", "/portal/marks",
                "/portal/videos", "/portal/feedback"]:
        response = client.get(url, follow_redirects=True)
        assert response.status_code == 200, url


def test_report_pdfs_generate(client, data):
    login(client, "admin", "admin123")
    for url in ["/admin/reports/finance/pdf", "/admin/reports/pending-fees/pdf",
                "/admin/reports/class-wise/pdf", "/admin/reports/student-wise/pdf"]:
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.data[:4] == b"%PDF", url


def test_exam_report_and_exports(client, data):
    login(client, "admin", "admin123")
    exam_id = data["exam_id"]
    assert client.get(f"/admin/exams/{exam_id}/report").status_code == 200
    assert client.get(f"/admin/exams/{exam_id}/report/pdf").data[:4] == b"%PDF"
    assert client.get(f"/admin/exams/{exam_id}/report/excel").status_code == 200
