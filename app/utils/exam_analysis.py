"""Exam result computation: per-student totals/grade/rank and per-subject
stats, shared by the on-screen analysis page, the PDF report card, and the
PDF class analysis report so all three always agree."""
from ..models import ExamMark, grade_for_percentage
from .charts import bar_chart, meter_chart, trend_chart, grouped_bar_chart


def compute_exam_results(exam, students):
    """Returns a list of dicts (one per student), ranked best-first by total
    marks. A student is 'Incomplete' until every exam subject has a mark
    recorded for them - incomplete students are excluded from ranking and
    sorted to the end."""
    exam_subjects = exam.exam_subjects
    total_max = sum(es.max_marks for es in exam_subjects)

    results = []
    for student in students:
        marks_by_subject = {
            es.id: ExamMark.query.filter_by(exam_subject_id=es.id, student_id=student.id).first()
            for es in exam_subjects
        }
        entered = [m for m in marks_by_subject.values() if m is not None]
        complete = bool(exam_subjects) and len(entered) == len(exam_subjects)
        total_obtained = sum(m.marks_obtained for m in entered)
        percentage = round(total_obtained / total_max * 100, 1) if total_max else 0
        failed_subjects = [
            marks_by_subject[es.id].exam_subject.subject.name
            for es in exam_subjects
            if marks_by_subject[es.id] and not marks_by_subject[es.id].is_pass
        ]

        if not complete:
            status = "Incomplete"
        elif failed_subjects:
            status = "Fail"
        else:
            status = "Pass"

        results.append(
            {
                "student": student,
                "marks_by_subject": marks_by_subject,
                "total_obtained": total_obtained,
                "total_max": total_max,
                "percentage": percentage,
                "grade": grade_for_percentage(percentage) if complete else "-",
                "failed_subjects": failed_subjects,
                "status": status,
                "complete": complete,
                "rank": None,
            }
        )

    complete_results = sorted(
        [r for r in results if r["complete"]], key=lambda r: r["total_obtained"], reverse=True
    )
    rank, prev_score = 0, None
    for i, r in enumerate(complete_results, start=1):
        if r["total_obtained"] != prev_score:
            rank = i
        r["rank"] = rank
        prev_score = r["total_obtained"]

    results.sort(key=lambda r: (r["rank"] is None, r["rank"] if r["rank"] is not None else 0))
    return results


def subject_analysis(exam, student_ids):
    """Per-subject stats (average %, highest, lowest, pass %) across the
    given student ids."""
    stats = []
    for es in exam.exam_subjects:
        marks = ExamMark.query.filter(
            ExamMark.exam_subject_id == es.id, ExamMark.student_id.in_(student_ids)
        ).all()
        if marks:
            average_pct = round(sum(m.percentage for m in marks) / len(marks), 1)
            highest = max(m.marks_obtained for m in marks)
            lowest = min(m.marks_obtained for m in marks)
            pass_pct = round(sum(1 for m in marks if m.is_pass) / len(marks) * 100, 1)
        else:
            average_pct = highest = lowest = pass_pct = 0

        stats.append(
            {
                "subject": es.subject.name,
                "exam_subject": es,
                "average_pct": average_pct,
                "highest": highest,
                "lowest": lowest,
                "pass_pct": pass_pct,
                "entered_count": len(marks),
            }
        )
    return stats


def build_report_context(exam, students):
    """Full analysis bundle (results, subject stats, pass/fail counts and
    chart images) for the given exam/students - used identically by the
    on-screen report, the PDF analysis report, and both admin/teacher
    blueprints so they can never disagree."""
    results = compute_exam_results(exam, students)
    stats = subject_analysis(exam, [s.id for s in students])
    complete_results = [r for r in results if r["complete"]]
    pass_count = sum(1 for r in complete_results if r["status"] == "Pass")
    fail_count = sum(1 for r in complete_results if r["status"] == "Fail")

    charts = {}
    if stats:
        charts["subject_avg"] = bar_chart(
            [s["subject"] for s in stats], [s["average_pct"] for s in stats], "Average Marks by Subject (%)"
        )
    if complete_results:
        charts["pass_fail"] = meter_chart(pass_count, fail_count, "Pass rate")

    return {
        "results": results,
        "stats": stats,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "incomplete_count": len(results) - len(complete_results),
        "charts": charts,
    }


# ------------------------------------------------- per-student progress
IMPROVED = "improved"
DECLINED = "declined"
STEADY = "steady"


def _trend(delta):
    if delta >= 3:
        return IMPROVED
    if delta <= -3:
        return DECLINED
    return STEADY


def student_progress(student, exams):
    """One student's results across several exams, oldest first, with
    subject-level movement and suggested next steps.

    Only exams where every subject has a mark are included - a half-entered
    exam would otherwise read as a collapse in performance.
    """
    from ..models import ExamMark

    timeline = []
    for exam in sorted(exams, key=lambda e: e.exam_date):
        result = compute_exam_results(exam, [student])[0]
        if not result["complete"]:
            continue

        subjects = {}
        for exam_subject in exam.exam_subjects:
            mark = ExamMark.query.filter_by(
                exam_subject_id=exam_subject.id, student_id=student.id
            ).first()
            if mark and exam_subject.max_marks:
                subjects[exam_subject.subject.name] = round(
                    mark.marks_obtained / exam_subject.max_marks * 100, 1
                )

        timeline.append({"exam": exam, "result": result, "subjects": subjects})

    if not timeline:
        return None

    latest = timeline[-1]
    previous = timeline[-2] if len(timeline) > 1 else None

    overall_delta = (
        round(latest["result"]["percentage"] - previous["result"]["percentage"], 1)
        if previous
        else None
    )

    movement = []
    if previous:
        for subject, pct in latest["subjects"].items():
            if subject in previous["subjects"]:
                delta = round(pct - previous["subjects"][subject], 1)
                movement.append(
                    {
                        "subject": subject,
                        "previous": previous["subjects"][subject],
                        "latest": pct,
                        "delta": delta,
                        "trend": _trend(delta),
                    }
                )
        movement.sort(key=lambda m: m["delta"])

    charts = {}
    if len(timeline) > 1:
        charts["trend"] = trend_chart(
            [t["exam"].name for t in timeline],
            [t["result"]["percentage"] for t in timeline],
            f"{student.name} - overall percentage by exam",
        )
        subjects = sorted(latest["subjects"].keys())
        charts["subjects"] = grouped_bar_chart(
            subjects,
            [
                (t["exam"].name, [t["subjects"].get(s, 0) for s in subjects])
                for t in timeline[-3:]
            ],
            f"{student.name} - subject marks by exam",
        )
    elif latest["subjects"]:
        subjects = sorted(latest["subjects"].keys())
        charts["subjects"] = bar_chart(
            subjects,
            [latest["subjects"][s] for s in subjects],
            f"{student.name} - {latest['exam'].name} by subject",
            ylabel="Marks (%)",
        )

    return {
        "timeline": timeline,
        "latest": latest,
        "previous": previous,
        "overall_delta": overall_delta,
        "overall_trend": _trend(overall_delta) if overall_delta is not None else None,
        "movement": movement,
        "charts": charts,
        "suggestions": _suggestions(latest, movement, overall_delta),
    }


def _suggestions(latest, movement, overall_delta):
    """Plain next steps a teacher or parent can act on.

    Deliberately rule-based and specific to this student's own numbers - a
    generic "study harder" tells nobody anything.
    """
    tips = []
    subjects = latest["subjects"]
    failed = latest["result"]["failed_subjects"]

    if failed:
        tips.append(
            {
                "level": "critical",
                "text": (
                    f"Not passed in {', '.join(failed)}. Arrange extra sessions in "
                    f"{'these subjects' if len(failed) > 1 else 'this subject'} before the "
                    "next exam, and re-test on the same syllabus to confirm the gap has closed."
                ),
            }
        )

    weakest = sorted(subjects.items(), key=lambda kv: kv[1])[:2]
    at_risk = [name for name, pct in weakest if 35 <= pct < 50 and name not in failed]
    if at_risk:
        tips.append(
            {
                "level": "warning",
                "text": (
                    f"{', '.join(at_risk)} {'are' if len(at_risk) > 1 else 'is'} only just "
                    "above the pass mark. A weekly practice set here would lift the total "
                    "more than further work on stronger subjects."
                ),
            }
        )

    dropped = [m for m in movement if m["trend"] == DECLINED]
    if dropped:
        worst = dropped[0]
        tips.append(
            {
                "level": "warning",
                "text": (
                    f"{worst['subject']} fell {abs(worst['delta']):.0f} points since the last "
                    f"exam ({worst['previous']:.0f}% to {worst['latest']:.0f}%). Worth asking "
                    "the student what changed - a specific topic, or missed classes."
                ),
            }
        )

    gained = [m for m in movement if m["trend"] == IMPROVED]
    if gained:
        best = gained[-1]
        tips.append(
            {
                "level": "good",
                "text": (
                    f"{best['subject']} improved by {best['delta']:.0f} points "
                    f"({best['previous']:.0f}% to {best['latest']:.0f}%). Whatever changed "
                    "here is worth repeating in the weaker subjects."
                ),
            }
        )

    if overall_delta is not None and overall_delta <= -5:
        tips.append(
            {
                "level": "critical",
                "text": (
                    f"Overall percentage is down {abs(overall_delta):.0f} points across all "
                    "subjects, not just one - check attendance and whether anything outside "
                    "class is affecting the student."
                ),
            }
        )
    elif overall_delta is not None and overall_delta >= 5:
        tips.append(
            {
                "level": "good",
                "text": f"Overall percentage is up {overall_delta:.0f} points. Steady progress.",
            }
        )

    spread = max(subjects.values()) - min(subjects.values()) if subjects else 0
    if spread >= 40:
        tips.append(
            {
                "level": "warning",
                "text": (
                    f"There is a {spread:.0f}-point gap between the best and weakest subject. "
                    "Time is better spent on the weakest two than on the subjects already scoring well."
                ),
            }
        )

    if not tips:
        tips.append(
            {
                "level": "good",
                "text": "Passing every subject with no significant drop. Keep the current routine.",
            }
        )
    return tips
