"""Hermes agents — the reasoning layer between the scrapers and the database.

Five agents, each with one job:

============================  ==========================================================
:class:`ProfileAnalyst`       Turn a messy scraped LinkedIn profile into structured
                              facts: skills, domains, achievements, a keyword bank
                              and honest gaps.
:class:`ResumeArchitect`      Write an ATS-parseable resume in markdown from those
                              facts, optionally targeted at one job posting.
:func:`score_resume`          Score a resume for ATS friendliness — deterministic
                              checks plus an LLM semantic pass.
:class:`JobScout`             Search LinkedIn through the MCP server and normalise
                              the results into job records with apply URLs.
:class:`MatchRanker`          Score one job against the analysed profile; its output
                              drives the ranking on the Jobs page.
============================  ==========================================================

Two rules hold across all of them:

* **No fabrication.** An agent may reorganise, prioritise and re-word the user's
  facts. It may never invent an employer, date, degree, certification or metric.
  ``ResumeArchitect`` additionally verifies its own output against the source
  data and reports anything it could not ground.
* **No silent failure.** Agents emit progress through ``Agent.emit`` so the
  dashboard's live log shows what happened, and they degrade to deterministic
  behaviour rather than raising when a free-tier model is unavailable.

Note on ``score_resume``: the ATS score is a *heuristic proxy* built from
published ATS parsing guidance (single column, canonical headings, no tables or
graphics, keyword coverage against the job description). It is not the output of
any specific vendor's parser and no particular score guarantees an interview.
"""

from __future__ import annotations

from hermes.agents.ats import (
    ACTION_VERBS,
    CANONICAL_SECTIONS,
    OPTIONAL_SECTIONS,
    REQUIRED_SECTIONS,
    collect_bullets,
    extract_keywords,
    score_resume,
    score_resume_deterministic,
    split_sections,
)
from hermes.agents.base import Agent, AgentError
from hermes.agents.job_scout import JobScout
from hermes.agents.match_ranker import MatchRanker
from hermes.agents.profile_analyst import ProfileAnalyst
from hermes.agents.resume_architect import ResumeArchitect

__all__ = [
    # base
    "Agent",
    "AgentError",
    # agents
    "ProfileAnalyst",
    "ResumeArchitect",
    "JobScout",
    "MatchRanker",
    # ATS scoring (function-style, no agent instance needed)
    "score_resume",
    "score_resume_deterministic",
    # ATS building blocks, re-exported so callers need not reach into .ats
    "ACTION_VERBS",
    "CANONICAL_SECTIONS",
    "REQUIRED_SECTIONS",
    "OPTIONAL_SECTIONS",
    "split_sections",
    "collect_bullets",
    "extract_keywords",
]
