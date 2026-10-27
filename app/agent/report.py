"""Report schema, grounding checks and HTML rendering.

The LLM only fills the Pydantic schema below. Every claim is checked against
what the agent actually read before Jinja2 renders it to HTML.
"""

import pathlib

import jinja2
import pydantic

from app.agent import audience
from app.agent import github

TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"
MAX_READING_STEPS = 8
MAX_GLOSSARY_TERMS = 12
MAX_QUICK_START = 6


class Citation(pydantic.BaseModel):
    """A line range in a file that supports a claim."""

    path: str
    start_line: int = pydantic.Field(ge=1)
    end_line: int = pydantic.Field(ge=1)


class ReportSection(pydantic.BaseModel):
    """One explained topic with its evidence."""

    title: str
    content: str
    citations: list[Citation] = pydantic.Field(default_factory=list)


class ReadingStep(pydantic.BaseModel):
    """One file in the suggested reading order."""

    path: str = pydantic.Field(description="트리에 있는 파일 경로")
    reason: str = pydantic.Field(description="이 순서에 이 파일을 읽는 이유")


class GlossaryTerm(pydantic.BaseModel):
    """A project-specific or technical term explained for the reader."""

    term: str
    meaning: str


class ProjectReport(pydantic.BaseModel):
    """The structured guide the LLM produces."""

    project_name: str
    summary: str
    stack: list[str] = pydantic.Field(default_factory=list)
    quick_start: list[str] = pydantic.Field(
        default_factory=list,
        description=(
            "로컬에서 실행해 보는 셸 명령을 순서대로. 감지된 명령이나 파일에 "
            "그대로 적힌 명령만 쓰세요."
        ),
    )
    reading_order: list[ReadingStep] = pydantic.Field(
        default_factory=list,
        description="처음 읽는 사람이 따라갈 파일 순서 (3~8개)",
    )
    sections: list[ReportSection]
    glossary: list[GlossaryTerm] = pydantic.Field(
        default_factory=list,
        description="독자가 모를 만한 용어와 짧은 설명",
    )


def filter_citations(
    citations: list[Citation], files: dict[str, str]
) -> list[Citation]:
    """Keeps citations whose path and lines were actually read.

    Args:
        citations: Citations proposed by the LLM.
        files: Path to `N: text` numbered content the agent read.

    Returns:
        The citations that point at real, read lines, without duplicates.
    """
    line_sets = {
        path: github.numbered_line_set(text) for path, text in files.items()
    }
    valid: list[Citation] = []
    for citation in citations:
        lines = line_sets.get(citation.path)
        if (
            lines is not None
            and citation.start_line <= citation.end_line
            and citation.start_line in lines
            and citation.end_line in lines
            and citation not in valid
        ):
            valid.append(citation)
    return valid


def verify_citations(
    report: ProjectReport, files: dict[str, str]
) -> ProjectReport:
    """Drops unverifiable citations and sections left with no evidence.

    Args:
        report: The LLM output.
        files: Path to numbered content the agent read.

    Returns:
        A copy of the report with only grounded sections.
    """
    sections = []
    for section in report.sections:
        citations = filter_citations(section.citations, files)
        if citations:
            sections.append(section.model_copy(update={"citations": citations}))
    return report.model_copy(update={"sections": sections})


def verify_report(
    report: ProjectReport,
    files: dict[str, str],
    *,
    tree: list[str],
    known_commands: list[str],
) -> ProjectReport:
    """Grounds every part of the report in what the agent saw.

    Sections keep only citations to read lines. The reading order keeps
    only paths in the tree. Quick-start commands must be detected from a
    manifest or appear verbatim in a file that was read.

    Args:
        report: The LLM output.
        files: Path to numbered content the agent read.
        tree: The repository tree.
        known_commands: Commands detected from manifests.

    Returns:
        A verified copy of the report.
    """
    verified = verify_citations(report, files)
    tree_paths = set(tree)
    seen: set[str] = set()
    steps = []
    for step in report.reading_order:
        if step.path in tree_paths and step.path not in seen:
            seen.add(step.path)
            steps.append(step)

    texts = list(files.values())
    commands: list[str] = []
    for command in report.quick_start:
        command = command.strip()
        grounded = command in known_commands or any(
            command in text for text in texts
        )
        if command and grounded and command not in commands:
            commands.append(command)

    return verified.model_copy(
        update={
            "reading_order": steps[:MAX_READING_STEPS],
            "quick_start": commands[:MAX_QUICK_START],
            "glossary": report.glossary[:MAX_GLOSSARY_TERMS],
        }
    )


def render_report(
    report: ProjectReport,
    *,
    repo: str | None = None,
    commit_sha: str | None = None,
    level: audience.Level = audience.DEFAULT_LEVEL,
) -> str:
    """Renders a verified report to a standalone HTML page.

    Args:
        report: A verified report.
        repo: Repository in `owner/name` form. Enables github.com links.
        commit_sha: Commit the links point at.
        level: Reader level shown in the header.

    Returns:
        The HTML document. All report text is auto-escaped.
    """
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(("html", "xml")),
    )

    def link(path: str, start: int | None = None, end: int | None = None):
        if not repo:
            return None
        return github.blob_url(repo, commit_sha, path, start, end)

    template = environment.get_template("report.html")
    return template.render(
        report=report,
        repo=repo,
        commit_sha=commit_sha,
        level_label=audience.LABELS[level],
        link=link,
    )
