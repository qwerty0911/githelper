"""Command-line entry point that writes a report to an HTML file."""

import argparse
import asyncio
import pathlib
from typing import get_args

from app.agent import audience
from app.agent import github
from app.agent import runner


async def run(
    url: str,
    branch: str | None,
    output: pathlib.Path,
    level: audience.Level = audience.DEFAULT_LEVEL,
) -> None:
    """Generates a report and writes it to `output`.

    Args:
        url: Repository URL.
        branch: Optional branch.
        output: Destination HTML file.
        level: Reader level.
    """
    repository = github.parse_repository_url(url, branch)
    result = await runner.generate_report(repository, level=level)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.html, encoding="utf-8")
    print(f"Report written to {output} (commit {result.commit_sha})")


def main() -> None:
    """Parses command-line flags and runs the agent."""
    parser = argparse.ArgumentParser(
        description="Generate a beginner-friendly GitHub project guide."
    )
    parser.add_argument("url", help="Public GitHub repository URL")
    parser.add_argument(
        "--branch",
        help="Branch to analyze (defaults to the repository default)",
    )
    parser.add_argument(
        "--level",
        choices=get_args(audience.Level),
        default=audience.DEFAULT_LEVEL,
        help="Reader level the guide is written for",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("reports/report.html"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.url, args.branch, args.output, args.level))


if __name__ == "__main__":
    main()
