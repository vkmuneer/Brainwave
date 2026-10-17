"""Editing a subject's max/pass marks after it has been added to an exam."""
from conftest import login


def _exam_subject(app):
    from app.models import ExamSubject
    with app.app_context():
        es = ExamSubject.query.first()
        return es.exam_id, es.id


def test_admin_can_change_max_and_pass_marks(client, app, data):
    from app.models import ExamSubject

    exam_id, es_id = _exam_subject(app)
    login(client, "admin", "admin123")
    # Raise to 120/40 - above the fixture's recorded mark of 90, so it is allowed.
    client.post(f"/admin/exams/{exam_id}/subjects/{es_id}/edit",
                data={"max_marks": 120, "pass_marks": 40}, follow_redirects=True)
    with app.app_context():
        es = ExamSubject.query.get(es_id)
        assert es.max_marks == 120
        assert es.pass_marks == 40


def test_pass_above_max_is_refused(client, app, data):
    from app.models import ExamSubject

    exam_id, es_id = _exam_subject(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/exams/{exam_id}/subjects/{es_id}/edit",
                data={"max_marks": 40, "pass_marks": 60}, follow_redirects=True)
    with app.app_context():
        es = ExamSubject.query.get(es_id)
        assert es.max_marks == 100  # unchanged from the fixture default


def test_cannot_lower_max_below_an_existing_mark(client, app, data):
    # The fixture records a mark of 90 for this subject.
    from app.models import ExamSubject

    exam_id, es_id = _exam_subject(app)
    login(client, "admin", "admin123")
    resp = client.post(f"/admin/exams/{exam_id}/subjects/{es_id}/edit",
                       data={"max_marks": 50, "pass_marks": 20}, follow_redirects=True)
    assert "already have more than that" in resp.get_data(as_text=True)
    with app.app_context():
        assert ExamSubject.query.get(es_id).max_marks == 100


def test_office_cannot_edit_marks(client, app, data):
    exam_id, es_id = _exam_subject(app)
    login(client, "office1", "Office@123")
    resp = client.post(f"/admin/exams/{exam_id}/subjects/{es_id}/edit",
                       data={"max_marks": 50, "pass_marks": 20})
    assert resp.status_code == 403


def test_zero_or_negative_max_is_refused(client, app, data):
    from app.models import ExamSubject
    exam_id, es_id = _exam_subject(app)
    login(client, "admin", "admin123")
    client.post(f"/admin/exams/{exam_id}/subjects/{es_id}/edit",
                data={"max_marks": 0, "pass_marks": 0}, follow_redirects=True)
    with app.app_context():
        assert ExamSubject.query.get(es_id).max_marks == 100
