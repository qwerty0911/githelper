import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

TEMPLATE_DIR = Path(__file__).parent / "templates"


class Citation(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ReportSection(BaseModel):
    title: str
    content: str
    citations: list[Citation] = Field(default_factory=list)


class ProjectReport(BaseModel):
    project_name: str
    summary: str
    stack: list[str] = Field(default_factory=list)
    sections: list[ReportSection]


def verify_citations(report: ProjectReport, files: dict[str, str]) -> ProjectReport:
    verified_sections = []
    for section in report.sections:
        valid_citations = []
        for citation in section.citations:
            content = files.get(citation.path)
            if content is None:
                continue
            file_lines = {
                int(match.group(1))
                for line in content.splitlines()
                if (match := re.match(r"^(\d+): ", line))
            }
            if (
                citation.start_line <= citation.end_line
                and citation.start_line in file_lines
                and citation.end_line in file_lines
            ):
                valid_citations.append(citation)
        if valid_citations:
            verified_sections.append(
                section.model_copy(update={"citations": valid_citations})
            )
    return report.model_copy(update={"sections": verified_sections})


def render_report(report: ProjectReport) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html", "xml")),
    )
    template = environment.get_template("report.html")
    return template.render(report=report)
