from app.agent.report import Citation, ProjectReport, ReportSection, render_report, verify_citations


def test_verify_citations_removes_missing_paths_and_invalid_lines():
    report = ProjectReport(
        project_name="demo",
        summary="summary",
        sections=[
            ReportSection(
                title="entrypoint",
                content="This is the entry point.",
                citations=[
                    Citation(path="main.py", start_line=1, end_line=2),
                    Citation(path="missing.py", start_line=1, end_line=1),
                    Citation(path="main.py", start_line=1, end_line=3),
                ],
            ),
            ReportSection(
                title="uncited",
                content="unverified",
                citations=[Citation(path="missing.py", start_line=1, end_line=1)],
            ),
        ],
    )

    verified = verify_citations(report, {"main.py": "1: one\n2: two\n"})

    assert len(verified.sections) == 1
    assert verified.sections[0].citations == [
        Citation(path="main.py", start_line=1, end_line=2)
    ]


def test_render_report_escapes_untrusted_content():
    report = ProjectReport(
        project_name="<script>alert(1)</script>",
        summary="A safe summary",
        sections=[
            ReportSection(
                title="Overview",
                content="<img src=x onerror=alert(1)>",
                citations=[Citation(path="README.md", start_line=1, end_line=1)],
            )
        ],
    )

    html = render_report(report)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
