import os
from typing import Any


def tracing_config(*, repo: str, branch: str | None) -> dict[str, Any]:
    """LangChain run config that sends traces to Langfuse and/or LangSmith when configured.

    LangSmith is enabled purely by env vars (LANGSMITH_TRACING, LANGSMITH_API_KEY), so only
    the run metadata is set here. Langfuse needs an explicit callback handler.
    """
    config: dict[str, Any] = {
        "run_name": "github_helper_report",
        "tags": ["github-helper"],
        "metadata": {"repo": repo, "branch": branch or "default"},
        "callbacks": [],
    }
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        from langfuse.langchain import CallbackHandler

        config["callbacks"].append(CallbackHandler())
    return config


def flush_traces() -> None:
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        from langfuse import get_client

        get_client().flush()
