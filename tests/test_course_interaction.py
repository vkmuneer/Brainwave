"""Questions and assignments inside a paid course."""
import io
import struct
import zlib
from datetime import date, timedelta

from conftest import login


def _png():
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes([10, 20, 30] * 4) for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _course_with_subscriber(app, client, mobile="9555000001", active=True):
    from app.extensions import db
    from app.models import Course, CourseVideo, Subscriber, Subscription

    with app.app_context():
        course = Course(title="Interactive Course", price=100, duration_days=30, published=True)
        db.session.add(course)
        db.session.flush()
        db.session.add(CourseVideo(course_id=course.id, title="L1",
                                   url="https://youtu.be/abc12345", sequence=1))
        db.session.commit()
        course_id = course.id

    client.post("/learn/register",
                data={"name": "Learner", "mobile": mobile, "password": "Pass@1234"})

    with app.app_context():
        subscriber = Subscriber.query.filter_by(mobile="91" + mobile).first()
        subscription = Subscription(
            subscriber_id=subscriber.id, course_id=course_id,
            status="active" if active else "pending", amount=100,
            starts_on=date.today(),
            expires_on=date.today() + timedelta(days=30) if active else None,
        )
        db.session.add(subscription)
        db.session.commit()
        return course_id, subscriber.id


def test_subscriber_can_ask_a_question(client, app, data):
    from app.models import CourseQuestion

    course_id, _ = _course_with_subscriber(app, client)
    client.post(f"/learn/course/{course_id}/questions", data={"body": "Why is step 3 needed?"})

    with app.app_context():
        question = CourseQuestion.query.first()
        assert question is not None
        assert question.is_answered is False


def test_a_lapsed_subscriber_cannot_ask(client, app, data):
    from app.models import CourseQuestion

    course_id, _ = _course_with_subscriber(app, client, active=False)
    client.post(f"/learn/course/{course_id}/questions", data={"body": "Let me in"})

    with app.app_context():
        assert CourseQuestion.query.count() == 0


def test_answering_publishes_to_everyone_on_the_course(client, app, data):
    from app.models import CourseQuestion

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000002")
    client.post(f"/learn/course/{course_id}/questions", data={"body": "Shared doubt"})

    with app.app_context():
        question_id = CourseQuestion.query.first().id

    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/course-questions/{question_id}/answer",
               data={"answer": "Because of the remainder."})

    # A second subscriber on the same course sees the answered question.
    other = app.test_client()
    other.post("/learn/register",
               data={"name": "Other", "mobile": "9555000003", "password": "Pass@1234"})
    from app.extensions import db
    from app.models import Subscriber, Subscription

    with app.app_context():
        them = Subscriber.query.filter_by(mobile="919555000003").first()
        db.session.add(Subscription(
            subscriber_id=them.id, course_id=course_id, status="active", amount=100,
            starts_on=date.today(), expires_on=date.today() + timedelta(days=30),
        ))
        db.session.commit()

    body = other.get(f"/learn/course/{course_id}/questions").get_data(as_text=True)
    assert "Because of the remainder." in body


def test_an_unanswered_question_stays_private(client, app, data):
    course_id, _ = _course_with_subscriber(app, client, mobile="9555000004")
    client.post(f"/learn/course/{course_id}/questions", data={"body": "My private doubt"})

    from app.extensions import db
    from app.models import Subscriber, Subscription

    other = app.test_client()
    other.post("/learn/register",
               data={"name": "Nosy", "mobile": "9555000005", "password": "Pass@1234"})
    with app.app_context():
        them = Subscriber.query.filter_by(mobile="919555000005").first()
        db.session.add(Subscription(
            subscriber_id=them.id, course_id=course_id, status="active", amount=100,
            starts_on=date.today(), expires_on=date.today() + timedelta(days=30),
        ))
        db.session.commit()

    body = other.get(f"/learn/course/{course_id}/questions").get_data(as_text=True)
    assert "My private doubt" not in body


def test_submitting_and_marking_an_assignment(client, app, data):
    from app.extensions import db
    from app.models import Assignment, AssignmentSubmission

    course_id, subscriber_id = _course_with_subscriber(app, client, mobile="9555000006")

    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Exercise 1", "max_marks": 20, "published": "1"})

    with app.app_context():
        assignment_id = Assignment.query.first().id

    client.post(
        f"/learn/assignment/{assignment_id}",
        data={"answer_text": "My working out", "attachment": (io.BytesIO(_png()), "work.png")},
        content_type="multipart/form-data",
    )
    with app.app_context():
        submission = AssignmentSubmission.query.first()
        assert submission.answer_text == "My working out"
        assert submission.attachment_data is not None
        assert submission.is_evaluated is False
        submission_id = submission.id

    staff.post(f"/admin/submissions/{submission_id}/evaluate",
               data={"marks": 17, "remarks": "Good, check step 4."})

    with app.app_context():
        submission = AssignmentSubmission.query.get(submission_id)
        assert submission.marks == 17
        assert submission.evaluated_by == "Administrator"

    body = client.get(f"/learn/assignment/{assignment_id}").get_data(as_text=True)
    assert "17" in body and "Good, check step 4." in body


def test_marks_above_the_maximum_are_refused(client, app, data):
    from app.models import Assignment, AssignmentSubmission

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000007")
    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Ex", "max_marks": 10, "published": "1"})
    with app.app_context():
        assignment_id = Assignment.query.first().id

    client.post(f"/learn/assignment/{assignment_id}", data={"answer_text": "x"})
    with app.app_context():
        submission_id = AssignmentSubmission.query.first().id

    staff.post(f"/admin/submissions/{submission_id}/evaluate", data={"marks": 99})
    with app.app_context():
        assert AssignmentSubmission.query.get(submission_id).marks is None


def test_marked_work_cannot_be_rewritten(client, app, data):
    from app.models import Assignment, AssignmentSubmission

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000008")
    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Ex", "max_marks": 10, "published": "1"})
    with app.app_context():
        assignment_id = Assignment.query.first().id

    client.post(f"/learn/assignment/{assignment_id}", data={"answer_text": "first answer"})
    with app.app_context():
        submission_id = AssignmentSubmission.query.first().id
    staff.post(f"/admin/submissions/{submission_id}/evaluate", data={"marks": 5})

    client.post(f"/learn/assignment/{assignment_id}", data={"answer_text": "changed after marking"})
    with app.app_context():
        assert AssignmentSubmission.query.get(submission_id).answer_text == "first answer"


def test_another_subscribers_attachment_is_refused(client, app, data):
    from app.models import Assignment, AssignmentSubmission

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000009")
    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Ex", "max_marks": 10, "published": "1"})
    with app.app_context():
        assignment_id = Assignment.query.first().id

    client.post(
        f"/learn/assignment/{assignment_id}",
        data={"answer_text": "mine", "attachment": (io.BytesIO(_png()), "w.png")},
        content_type="multipart/form-data",
    )
    with app.app_context():
        submission_id = AssignmentSubmission.query.first().id

    other = app.test_client()
    other.post("/learn/register",
               data={"name": "Other", "mobile": "9555000010", "password": "Pass@1234"})
    assert other.get(f"/learn/submission/{submission_id}/attachment").status_code == 403


def test_only_admin_can_mark(client, app, data):
    from app.models import Assignment, AssignmentSubmission

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000011")
    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Ex", "max_marks": 10, "published": "1"})
    with app.app_context():
        assignment_id = Assignment.query.first().id
    client.post(f"/learn/assignment/{assignment_id}", data={"answer_text": "x"})
    with app.app_context():
        submission_id = AssignmentSubmission.query.first().id

    office = app.test_client()
    login(office, "office1", "Office@123")
    assert office.post(f"/admin/submissions/{submission_id}/evaluate",
                       data={"marks": 5}).status_code == 403


def test_admin_pages_render(client, app, data):
    """The queue, the assignment list and the submission list all draw."""
    from app.models import Assignment, AssignmentSubmission, CourseQuestion

    course_id, _ = _course_with_subscriber(app, client, mobile="9555000012")
    client.post(f"/learn/course/{course_id}/questions", data={"body": "A doubt"})

    staff = app.test_client()
    login(staff, "admin", "admin123")
    staff.post(f"/admin/courses/{course_id}/assignments",
               data={"title": "Ex", "max_marks": 10, "published": "1"})
    with app.app_context():
        assignment_id = Assignment.query.first().id
    client.post(f"/learn/assignment/{assignment_id}", data={"answer_text": "done"})

    queue = staff.get("/admin/course-questions")
    assert queue.status_code == 200
    assert "A doubt" in queue.get_data(as_text=True)

    assignments = staff.get(f"/admin/courses/{course_id}/assignments")
    assert assignments.status_code == 200
    assert "Ex" in assignments.get_data(as_text=True)

    submissions = staff.get(f"/admin/assignments/{assignment_id}/submissions")
    assert submissions.status_code == 200
    assert "done" in submissions.get_data(as_text=True)

    with app.app_context():
        assert CourseQuestion.query.count() == 1
        assert AssignmentSubmission.query.count() == 1
