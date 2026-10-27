import argparse
import asyncio
from pathlib import Path

from app.agent.github import parse_repository_url
from app.agent.runner import generate_report


async def run(url: str, branch: str | None, output: Path) -> None:
    repository = parse_repository_url(url, branch)
    result = await generate_report(repository)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.html, encoding="utf-8")
    print(f"Report written to {output} (commit {result.commit_sha})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a beginner-friendly GitHub project guide."
    )
    parser.add_argument("url", help="Public GitHub repository URL")
    parser.add_argument("--branch", help="Branch to analyze (defaults to the repository default)")
    parser.add_argument("--output", type=Path, default=Path("reports/report.html"))
    args = parser.parse_args()
    asyncio.run(run(args.url, args.branch, args.output))


if __name__ == "__main__":
    main()
