"""Optional tracing to Langfuse and/or LangSmith."""

import os
from typing import Any


def _langfuse_enabled() -> bool:
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )


def tracing_config(
    *,
    repo: str,
    branch: str | None,
    run_name: str = "github_helper_report",
) -> dict[str, Any]:
    """Builds a LangChain run config that sends traces when configured.

    LangSmith is enabled purely by environment variables
    (`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`), so only run metadata is set
    for it. Langfuse needs an explicit callback handler.

    Args:
        repo: Repository in `owner/name` form, recorded as metadata.
        branch: Branch, recorded as metadata.
        run_name: Name of the top-level trace.

    Returns:
        A config dict for `ainvoke` / `astream`.
    """
    config: dict[str, Any] = {
        "run_name": run_name,
        "tags": ["github-helper"],
        "metadata": {"repo": repo, "branch": branch or "default"},
        "callbacks": [],
    }
    if _langfuse_enabled():
        # Imported lazily so Langfuse is only loaded when configured.
        from langfuse import langchain  # pylint: disable=import-outside-toplevel

        config["callbacks"].append(langchain.CallbackHandler())
    return config


def flush_traces() -> None:
    """Sends buffered Langfuse traces before the process moves on."""
    if _langfuse_enabled():
        import langfuse  # pylint: disable=import-outside-toplevel

        langfuse.get_client().flush()
