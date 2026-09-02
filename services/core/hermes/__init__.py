"""Hermes — self-hosted LinkedIn profile → ATS resume → ranked jobs agent.

This package is the `hermes-core` service: a FastAPI orchestrator that drives
LLM calls (through the self-hosted freellmapi router), LinkedIn scraping
(through the linkedin-mcp MCP server) and ephemeral Docker sandboxes for code
execution / document rendering.

Hermes NEVER submits a job application. It produces a ranked job list, a
tailored ATS-optimised resume and a direct apply URL; a human clicks Apply.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
