"""MatchRanker — score one job posting against the candidate's analysed profile.

Called once per discovered job by ``pipeline._rank_jobs`` (under bounded
concurrency), and its ``score`` becomes ``Job.match_score``, which is the column
the Jobs page sorts on. That makes this the agent that decides what the user
actually sees first, so two properties matter more than cleverness:

* **Stability.** The same job and profile should produce close to the same
  score. Free-tier models are noisy, so the LLM is asked for evidence
  (matched/missing skills, seniority fit) and the *arithmetic* is done here.
  A model returning only a bare number would make the ranking jitter between
  runs and the ordering untrustworthy.
* **Survivability.** A single unrankable job must not lose the whole search.
  Every failure path degrades to a deterministic keyword-overlap score rather
  than raising, so the job still appears in the list — just ranked on less
  evidence, and honestly labelled as such.

The score is a *relevance* estimate for prioritising applications. It is not a
prediction of whether the user will be interviewed or hired.
"""

from __future__ import annotations

import logging
from typing import Any

from hermes.agents.ats import extract_keywords, stem, tokenize
from hermes.agents.base import Agent

log = logging.getLogger("hermes.agents.match_ranker")

__all__ = ["MatchRanker", "VERDICTS"]

#: Verdict bands, high to low. The thresholds are the boundaries between them.
VERDICTS: tuple[tuple[float, str], ...] = (
    (78.0, "strong"),
    (58.0, "good"),
    (38.0, "stretch"),
    (0.0, "poor"),
)

#: Weights for the blended score. Skill overlap dominates because it is the one
#: signal that is measurable from the text on both sides.
_W_SKILLS = 0.45
_W_SEMANTIC = 0.35
_W_SENIORITY = 0.20

_SENIORITY_RANK: dict[str, int] = {
    "intern": 0,
    "entry": 1,
    "junior": 1,
    "associate": 2,
    "mid": 3,
    "mid-level": 3,
    "intermediate": 3,
    "senior": 4,
    "staff": 5,
    "lead": 5,
    "principal": 6,
    "architect": 6,
    "manager": 5,
    "head": 6,
    "director": 7,
    "vp": 8,
    "executive": 8,
}

_SCHEMA_HINT = """Return ONE JSON object, no prose, with exactly these keys:
{
  "semantic_fit": <integer 0-100: how well this candidate fits this role overall>,
  "matched_skills": [<skills the candidate demonstrably has that the job asks for>],
  "missing_skills": [<skills the job asks for that the candidate has not evidenced>],
  "seniority_fit": "<one of: under|match|over>",
  "reasons": [<2-5 short factual sentences justifying the score>],
  "tailoring_notes": [<2-5 imperative instructions for rewriting the resume for THIS job>]
}
Rules:
- Judge only on the evidence given. Do not assume unstated skills.
- "matched_skills" must appear in the candidate data; never copy a requirement
  into matched_skills just because the job asks for it.
- "tailoring_notes" must be actionable rewrite instructions, e.g.
  "Lead with the Kafka pipeline work and quantify throughput", not vague advice.
"""


def _verdict_for(score: float) -> str:
    for threshold, label in VERDICTS:
        if score >= threshold:
            return label
    return "poor"


def _seniority_score(profile_level: str, job_text: str) -> tuple[float, str]:
    """
    Compare the candidate's seniority to the level implied by the job text.

    Returns ``(0-100, fit)`` where fit is under/match/over. Being slightly
    *over*-levelled is penalised far less than being under-levelled: a senior
    engineer can apply to a mid role, the reverse usually cannot.
    """
    cand = _SENIORITY_RANK.get(str(profile_level or "").strip().lower())

    low = (job_text or "").lower()
    job_level: int | None = None
    for word, rank in _SENIORITY_RANK.items():
        if word in low:
            # Prefer the highest level mentioned; postings say "senior" once and
            # "junior" never, but a "senior or staff" posting should read staff.
            job_level = rank if job_level is None else max(job_level, rank)

    if cand is None or job_level is None:
        return 60.0, "match"  # unknown on either side: neutral, not punitive

    delta = cand - job_level
    if delta == 0:
        return 100.0, "match"
    if delta > 0:
        return max(55.0, 100.0 - 12.0 * delta), "over"
    return max(10.0, 100.0 + 22.0 * delta), "under"


def _candidate_terms(analysis: dict[str, Any]) -> set[str]:
    """Every stemmed skill/tool/domain token the candidate can claim."""
    buckets = ("hard_skills", "soft_skills", "tools", "domains", "certifications", "keyword_bank")
    terms: set[str] = set()
    for key in buckets:
        for item in Agent.str_list(analysis.get(key)):
            for token in tokenize(str(item)):
                terms.add(stem(token))
    return terms


def _overlap_score(analysis: dict[str, Any], job_text: str) -> tuple[float, list[str], list[str]]:
    """
    Deterministic keyword overlap between the job text and the candidate.

    This is both a component of the blended score and the fallback used when the
    LLM is unavailable, so it must stand on its own.
    """
    cand = _candidate_terms(analysis)
    if not cand:
        return 0.0, [], []

    keywords = extract_keywords(job_text, top_n=40)
    if not keywords:
        return 0.0, [], []

    matched: list[str] = []
    missing: list[str] = []
    hit_weight = 0.0
    total_weight = 0.0

    for phrase, weight in keywords:
        total_weight += weight
        phrase_stems = {stem(t) for t in tokenize(phrase)}
        if phrase_stems and phrase_stems <= cand:
            hit_weight += weight
            matched.append(phrase)
        else:
            missing.append(phrase)

    pct = (hit_weight / total_weight * 100.0) if total_weight else 0.0
    return round(pct, 1), matched[:25], missing[:25]


class MatchRanker(Agent):
    """Rank one job against an analysed profile."""

    NAME = "match-ranker"

    SYSTEM = (
        "You are a senior technical recruiter assessing fit between one candidate "
        "and one job posting. You are blunt and evidence-driven: you never credit "
        "the candidate with a skill the evidence does not show, and you never pad "
        "a score to be encouraging. You answer only in the JSON format requested."
    )

    async def rank(self, job: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
        """
        Score ``job`` against ``analysis``.

        Returns a dict with ``score`` (0-100, the value stored on
        ``Job.match_score``), ``verdict``, ``reasons``, ``matched_skills``,
        ``missing_skills`` and ``tailoring_notes``. Never raises: on any LLM
        failure it falls back to the deterministic overlap score and records why
        in ``degraded``.
        """
        title = self.clean_str(job.get("title"), 200)
        company = self.clean_str(job.get("company"), 160)
        description = self.clean_str(job.get("description"), 9000)
        location = self.clean_str(job.get("location"), 160)

        job_text = "\n".join(filter(None, [title, company, location, description]))

        overlap, matched_kw, missing_kw = _overlap_score(analysis, job_text)
        seniority, seniority_fit = _seniority_score(analysis.get("seniority", ""), job_text)

        # A posting with no scraped description gives the model nothing to reason
        # about; the deterministic signals are all we honestly have.
        if len(description) < 120 or self.llm is None:
            reason = (
                "No job description was available to analyse"
                if len(description) < 120
                else "No LLM router is configured"
            )
            score = round(overlap * 0.75 + seniority * 0.25, 1)
            return {
                "score": score,
                "verdict": _verdict_for(score),
                "reasons": [f"{reason}; scored on keyword overlap and seniority only."],
                "matched_skills": matched_kw,
                "missing_skills": missing_kw,
                "seniority_fit": seniority_fit,
                "tailoring_notes": [],
                "degraded": reason,
                "components": {"skills": overlap, "semantic": None, "seniority": seniority},
            }

        prompt = self._build_prompt(analysis, title, company, location, description)

        try:
            data = await self.ask_json(prompt, schema_hint=_SCHEMA_HINT, temperature=0.1)
        except Exception as exc:
            log.warning("MatchRanker LLM call failed for %r: %s", title, exc)
            await self.emit(f"Ranking '{title}' without the LLM ({exc}).", "warn")
            score = round(overlap * 0.75 + seniority * 0.25, 1)
            return {
                "score": score,
                "verdict": _verdict_for(score),
                "reasons": ["Scored on keyword overlap and seniority; the LLM call failed."],
                "matched_skills": matched_kw,
                "missing_skills": missing_kw,
                "seniority_fit": seniority_fit,
                "tailoring_notes": [],
                "degraded": f"{type(exc).__name__}: {exc}",
                "components": {"skills": overlap, "semantic": None, "seniority": seniority},
            }

        semantic = self.clamp(data.get("semantic_fit"), 0.0, 100.0, default=overlap)

        # Prefer the model's skill lists (it reads phrasing the tokeniser misses)
        # but keep the deterministic lists when the model returns nothing.
        matched = self.str_list(data.get("matched_skills"), limit=25) or matched_kw
        missing = self.str_list(data.get("missing_skills"), limit=25) or missing_kw

        llm_fit = str(data.get("seniority_fit") or "").strip().lower()
        if llm_fit in {"under", "match", "over"}:
            seniority_fit = llm_fit
            # Re-derive the numeric component from the model's judgement so the
            # two cannot disagree in the stored breakdown.
            seniority = {"match": 100.0, "over": 78.0, "under": 42.0}[llm_fit]

        score = round(
            overlap * _W_SKILLS + semantic * _W_SEMANTIC + seniority * _W_SENIORITY,
            1,
        )

        return {
            "score": score,
            "verdict": _verdict_for(score),
            "reasons": self.str_list(data.get("reasons"), limit=5),
            "matched_skills": matched,
            "missing_skills": missing,
            "seniority_fit": seniority_fit,
            "tailoring_notes": self.str_list(data.get("tailoring_notes"), limit=5),
            "components": {"skills": overlap, "semantic": semantic, "seniority": seniority},
        }

    def _build_prompt(
        self,
        analysis: dict[str, Any],
        title: str,
        company: str,
        location: str,
        description: str,
    ) -> str:
        """Compact, evidence-only prompt. Keeps free-tier token use sane."""
        candidate = {
            "headline": self.clean_str(analysis.get("headline"), 200),
            "seniority": self.clean_str(analysis.get("seniority"), 40),
            "years_experience": analysis.get("years_experience"),
            "domains": self.str_list(analysis.get("domains"), limit=12),
            "hard_skills": self.str_list(analysis.get("hard_skills"), limit=40),
            "tools": self.str_list(analysis.get("tools"), limit=30),
            "certifications": self.str_list(analysis.get("certifications"), limit=12),
            "achievements": [
                self.clean_str(a.get("text") if isinstance(a, dict) else a, 220)
                for a in self.as_list(analysis.get("achievements"))[:8]
            ],
        }
        return (
            "CANDIDATE (the only evidence you may use):\n"
            f"{self.compact_json(candidate, 6000)}\n\n"
            "JOB POSTING:\n"
            f"Title: {title}\n"
            f"Company: {company}\n"
            f"Location: {location}\n"
            f"Description:\n{description}\n\n"
            "Assess the fit and return the JSON object."
        )
