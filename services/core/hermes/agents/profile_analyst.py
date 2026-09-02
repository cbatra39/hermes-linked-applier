"""ProfileAnalyst -- turn a messy LinkedIn scrape into a structured career profile.

The input is whatever ``stickerdaniel/linkedin-mcp-server`` managed to scrape off
the page. That payload is *not* a stable contract: keys change with LinkedIn's
DOM, sections come back nested one or two levels down, lists arrive as lists of
dicts, lists of strings, or a dict keyed ``{"0": {...}, "1": {...}}``, and any
section the scraper could not reach is simply absent. Everything in this module
therefore assumes the worst and still produces the full analysis contract.

Two passes run over the profile:

1. **Deterministic extraction** (no LLM, no network): employers, titles, date
   ranges, total years of experience computed from merged employment intervals,
   declared skills, education, certifications, and a keyword bank mined with
   ``hermes.agents.ats.extract_keywords``. This always succeeds, even on an
   almost-empty profile.
2. **LLM interpretation**: seniority, positioning, domains, soft skills,
   achievements, gaps and target roles -- the judgement calls a regex cannot
   make.

The LLM answer is then merged *over* the deterministic facts, never the other
way round, and anything the model omitted falls back to pass 1. If the router is
down the analysis is still returned, marked ``analysis_mode="deterministic"``,
because a failed profile import would block the entire pipeline for what is
ultimately an enrichment step.

Anti-fabrication: the system prompt forbids inventing employers, titles, dates
and certifications, and the merge step only accepts model-supplied
``certifications`` entries that are actually present in the scraped text.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from hermes.agents.ats import extract_keywords
from hermes.agents.base import Agent

log = logging.getLogger(__name__)

__all__ = [
    "ProfileAnalyst",
    "ANALYSIS_KEYS",
    "empty_analysis",
    "extract_profile_facts",
]


#: The exact key set ``analyze()`` guarantees (mirrors ``schemas.ProfileAnalysis``).
ANALYSIS_KEYS: tuple[str, ...] = (
    "headline",
    "summary",
    "seniority",
    "years_experience",
    "domains",
    "hard_skills",
    "soft_skills",
    "tools",
    "certifications",
    "achievements",
    "keyword_bank",
    "gaps",
    "target_roles",
    "positioning_statement",
)


# --------------------------------------------------------------------------- #
# Scrape-shape tolerance
# --------------------------------------------------------------------------- #

#: Wrapper keys the MCP server (or a future version of it) may nest under.
_CONTAINER_KEYS: tuple[str, ...] = (
    "profile", "data", "result", "results", "person", "payload", "profile_data", "user",
)

#: Keys whose presence proves we have reached the dict that holds profile fields.
_PROFILE_MARKERS: frozenset[str] = frozenset(
    {
        "headline", "summary", "about", "aboutsection", "experience", "experiences",
        "education", "educations", "skills", "fullname", "name", "firstname",
        "lastname", "occupation", "certifications", "projects", "positions",
        "publicidentifier", "location", "title",
    }
)


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _unwrap(raw: Any) -> dict[str, Any]:
    """Descend through wrapper dicts until the real profile fields are in view."""
    body = raw if isinstance(raw, dict) else {}
    for _ in range(4):
        if any(_norm_key(k) in _PROFILE_MARKERS for k in body):
            return body
        nxt: dict[str, Any] | None = None
        for key in _CONTAINER_KEYS:
            inner = body.get(key)
            if isinstance(inner, dict) and inner:
                nxt = inner
                break
        if nxt is None:
            return body
        body = nxt
    return body


def _entries(body: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """Normalise a profile section into a list of dicts.

    Accepts a list of dicts, a list of strings, a single dict, or a
    ``{"0": {...}}`` style mapping (all of which have been seen in the wild).
    """
    out: list[dict[str, Any]] = []
    for item in Agent.as_list(Agent.first_of(body, *keys, default=None)):
        if isinstance(item, dict):
            if item:
                out.append(item)
        else:
            text = Agent.clean_str(item, 400)
            if text:
                out.append({"title": text})
    return out


# --------------------------------------------------------------------------- #
# Dates -> years of experience
# --------------------------------------------------------------------------- #

_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_YEAR_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s*"
    r"(?:of\s+)?((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])[/\-]((?:19|20)\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(0[1-9]|1[0-2])(?:-\d{2})?\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_PRESENT_RE = re.compile(
    r"\b(present|current(?:ly)?|now|to\s*date|till\s*date|ongoing|till\s*now)\b", re.IGNORECASE
)
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|–|—|~|\bto\b|\buntil\b|\bthrough\b)\s*", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:\+)?\s*(?:yrs?|years?))?\s*(?:(\d{1,2})\s*(?:mos?|months?))?", re.IGNORECASE
)

#: Metric shapes worth promoting into ``achievements[].metric``.
_METRIC_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)*\s*%"
    r"|[$€£₹¥]\s?\d+(?:[.,]\d+)*\s*(?:[KMB]|k|m|bn|billion|million|thousand|lakh|crore)?"
    r"|\b\d+(?:[.,]\d+)*\s*(?:[KMB]\b|x\b|bps\b|fte\b|hrs?\b|hours?\b|days?\b|weeks?\b|months?\b|years?\b)"
    r"|\b\d{2,}(?:[.,]\d{3})*\b)",
    re.IGNORECASE,
)

_SENIORITY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("executive", ("chief", "cxo", "ceo", "cto", "cio", "ciso", "cfo", "coo",
                   "president", "partner", "managing director", "vice president", "vp ", " vp")),
    ("director", ("director", "head of", "general manager")),
    ("manager", ("manager", "sr. manager", "senior manager", "team lead", "engineering lead",
                 "supervisor", "principal consultant")),
    ("principal", ("principal", "staff engineer", "distinguished", "architect", "fellow")),
    ("senior", ("senior", "sr.", "sr ", "lead ", "specialist", "consultant iii")),
    ("mid", ("associate", "analyst", "engineer", "developer", "consultant", "administrator")),
    ("junior", ("junior", "jr.", "jr ", "graduate", "trainee", "entry")),
    ("intern", ("intern", "internship", "apprentice", "student")),
)


def _month_index(year: int, month: int) -> int:
    return year * 12 + max(1, min(12, month)) - 1


def _parse_point(text: str) -> tuple[int, int] | None:
    """Parse a single date-ish string into ``(year, month)``."""
    raw = Agent.clean_str(text, 120)
    if not raw:
        return None
    match = _ISO_DATE_RE.search(raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _MONTH_YEAR_RE.search(raw)
    if match:
        return int(match.group(2)), _MONTH_NAMES.get(match.group(1).lower()[:4].rstrip("."), 1)
    match = _NUMERIC_DATE_RE.search(raw)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = _YEAR_RE.search(raw)
    if match:
        # A bare year is ambiguous; mid-year keeps the tenure estimate unbiased.
        return int(match.group(1)), 6
    return None


def _point_from_obj(value: Any) -> tuple[int, int] | None:
    """Parse LinkedIn's ``{"year": 2021, "month": 4}`` style date objects."""
    if isinstance(value, dict):
        year = Agent.first_of(value, "year", "yyyy")
        if year is None:
            return None
        try:
            year_int = int(str(year)[:4])
        except (TypeError, ValueError):
            return None
        month = Agent.first_of(value, "month", "mm", default=1)
        try:
            month_int = int(str(month)[:2])
        except (TypeError, ValueError):
            month_int = 1
        return year_int, month_int
    return _parse_point(Agent.clean_str(value, 120))


def _entry_interval(entry: dict[str, Any], now: tuple[int, int]) -> tuple[int, int] | None:
    """Return ``(start_month_index, end_month_index)`` for one experience entry."""
    start_raw = Agent.first_of(
        entry, "starts_at", "start_date", "startDate", "start", "from", "date_from", "began"
    )
    end_raw = Agent.first_of(
        entry, "ends_at", "end_date", "endDate", "end", "to", "date_to", "until"
    )
    combined = Agent.clean_str(
        Agent.first_of(
            entry, "dates", "date_range", "daterange", "duration", "period", "timeframe",
            "tenure", "employment_period", "date", default="",
        ),
        200,
    )

    start = _point_from_obj(start_raw) if start_raw is not None else None
    end = _point_from_obj(end_raw) if end_raw is not None else None
    is_current = bool(
        Agent.first_of(entry, "is_current", "current", "iscurrent", default=False)
    ) or bool(_PRESENT_RE.search(combined) or _PRESENT_RE.search(Agent.clean_str(end_raw, 60)))

    if start is None and combined:
        parts = [p for p in _RANGE_SPLIT_RE.split(combined) if p.strip()]
        if parts:
            start = _parse_point(parts[0])
            if len(parts) > 1 and end is None:
                end = _parse_point(parts[1])
    if start is None:
        return None
    if end is None:
        end = now if is_current else start

    start_idx = _month_index(*start)
    end_idx = _month_index(*end)
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx
    # Never let a stale scrape claim tenure into the future.
    end_idx = min(end_idx, _month_index(*now))
    return start_idx, max(start_idx, end_idx)


def _merged_months(intervals: Iterable[tuple[int, int]]) -> int:
    """Total months covered by (possibly overlapping) employment intervals."""
    ordered = sorted(intervals)
    total = 0
    cur_start: int | None = None
    cur_end: int | None = None
    for start, end in ordered:
        if cur_start is None or cur_end is None:
            cur_start, cur_end = start, end
            continue
        if start <= cur_end + 1:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start + 1
            cur_start, cur_end = start, end
    if cur_start is not None and cur_end is not None:
        total += cur_end - cur_start + 1
    return total


def _duration_months(text: str) -> int:
    """Parse a LinkedIn 'duration' string such as '3 yrs 2 mos'."""
    match = _DURATION_RE.search(Agent.clean_str(text, 80))
    if not match or not (match.group(1) or match.group(2)):
        return 0
    years = int(match.group(1) or 0)
    months = int(match.group(2) or 0)
    return years * 12 + months


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class ProfileAnalyst(Agent):
    """Structure a raw LinkedIn profile scrape into the Hermes analysis contract."""

    NAME = "profile-analyst"

    SYSTEM = """You are a senior technical recruiter and career strategist. You read a raw,
partially-scraped LinkedIn profile and turn it into a precise structured analysis that will
drive resume generation and job matching.

ABSOLUTE RULES
- Use ONLY facts present in the supplied profile. Never invent an employer, job title,
  date, degree, certification, employer size, or metric. Scraped profiles are frequently
  incomplete -- an absent fact stays absent.
- If a field cannot be supported by the input, return "" for strings, [] for lists and 0
  for numbers. An empty field is correct; a plausible guess is a defect.
- Prefer the candidate's own vocabulary (their tool names, domain terms, job titles) --
  those are the words recruiters and ATS keyword searches will match on.
- `gaps` must be honest and specific (missing evidence, unquantified impact, stale tooling,
  no leadership signal). It is feedback, not criticism theatre.
- `keyword_bank` is a recruiter search vocabulary: concrete skills, tools, platforms,
  standards, domains and role titles. No soft-skill fluff, no sentences.
- `achievements[].metric` is copied verbatim from the profile when a number is stated, and
  is null otherwise. Never derive, estimate or round a number that is not written down.
- Distinguish hard_skills (capabilities: "threat modelling", "SOX ITGC testing") from tools
  (named products: "Splunk", "Terraform") from soft_skills (behaviours).

Return ONLY the requested JSON object."""

    SCHEMA_HINT = """{
  "headline": "one line, <=120 chars, the candidate's professional identity",
  "summary": "3-5 sentences of factual career narrative, no pronouns, no hype",
  "seniority": "intern|junior|mid|senior|principal|manager|director|executive",
  "years_experience": 0.0,
  "domains": ["industry / functional domains, max 8"],
  "hard_skills": ["technical capabilities, max 25"],
  "soft_skills": ["behavioural strengths evidenced by the profile, max 10"],
  "tools": ["named products, platforms, frameworks, languages, max 30"],
  "certifications": ["exact certification names as written in the profile"],
  "achievements": [{"text": "what was accomplished", "metric": "verbatim number or null"}],
  "keyword_bank": ["ATS/recruiter search terms, max 45"],
  "gaps": ["specific, actionable weaknesses in this profile, max 8"],
  "target_roles": ["realistic job titles to search LinkedIn for, max 6"],
  "positioning_statement": "one sentence a recruiter could repeat to a hiring manager"
}"""

    #: Prompt-size ceilings. Free-tier models have small effective context windows
    #: and a truncated prompt beats a 413 from the router.
    MAX_DIGEST_CHARS = 11000
    MAX_RAW_JSON_CHARS = 6000

    async def analyze(self, raw_profile: dict) -> dict:
        """Analyse a scraped LinkedIn profile.

        Parameters
        ----------
        raw_profile:
            The payload returned by ``LinkedInMCP.get_my_profile`` /
            ``get_person_profile``. Any shape is tolerated, including ``{}``.

        Returns
        -------
        dict with every key in :data:`ANALYSIS_KEYS` (plus ``analysis_mode`` and
        ``evidence`` for diagnostics). Never raises for a sparse profile.
        """
        body = _unwrap(raw_profile)
        facts = self.extract_facts(body)

        if not facts["experience"] and not facts["skills"] and not facts["about"]:
            await self.emit(
                "Scraped profile is nearly empty (no experience, skills or about section). "
                "Analysis will be thin -- check the LinkedIn session and re-import.",
                "warn",
            )

        await self.emit(
            f"Parsed profile: {len(facts['experience'])} role(s), "
            f"{len(facts['education'])} education entr(y/ies), {len(facts['skills'])} skill(s), "
            f"{facts['years_experience']:.1f} yr(s) of merged tenure."
        )

        base = self._deterministic_analysis(facts)

        if self.llm is None:
            await self.emit(
                "No LLM router configured -- returning the deterministic profile analysis only.",
                "warn",
            )
            base["analysis_mode"] = "deterministic"
            base["gaps"] = base["gaps"] + [
                "LLM analysis was skipped: seniority, positioning and target roles are "
                "heuristic. Configure FREELLMAPI_KEY for a full analysis."
            ]
            return base

        prompt = self._build_prompt(facts)
        try:
            await self.emit("Interpreting the profile with the LLM…")
            data = await self.ask_json(
                prompt, schema_hint=self.SCHEMA_HINT, temperature=0.15, max_tokens=2600
            )
        except Exception as exc:
            log.warning("ProfileAnalyst LLM pass failed: %s", exc)
            await self.emit(
                f"LLM analysis failed ({type(exc).__name__}: {exc}). "
                "Falling back to the deterministic analysis so the import still completes.",
                "warn",
            )
            base["analysis_mode"] = "deterministic-fallback"
            base["gaps"] = base["gaps"] + [
                f"LLM analysis unavailable ({type(exc).__name__}); seniority, positioning and "
                "target roles below are heuristic."
            ]
            return base

        merged = self._merge(base, data, facts)
        merged["analysis_mode"] = "llm"
        await self.emit(
            f"Analysis ready — seniority '{merged['seniority']}', "
            f"{len(merged['keyword_bank'])} keywords, {len(merged['target_roles'])} target role(s)."
        )
        return merged

    # ----------------------------------------------------------- extraction

    @staticmethod
    def extract_facts(body: dict[str, Any]) -> dict[str, Any]:
        """Pull every fact we can prove out of the scrape (no LLM involved).

        Deliberately a staticmethod with no LLM/IO dependency: ``ResumeArchitect``
        reuses it (via :func:`extract_profile_facts`) so both agents see exactly
        the same interpretation of a raw profile.
        """
        now = datetime.now(timezone.utc)
        now_ym = (now.year, now.month)

        experience_raw = _entries(
            body, "experience", "experiences", "positions", "work_experience", "jobs", "roles"
        )
        education_raw = _entries(body, "education", "educations", "schools", "academics")
        cert_raw = _entries(
            body, "certifications", "certificates", "licenses", "licences",
            "licenses_and_certifications", "accomplishments_certifications",
        )
        project_raw = _entries(body, "projects", "accomplishment_projects", "portfolio")

        experience: list[dict[str, Any]] = []
        intervals: list[tuple[int, int]] = []
        duration_fallback = 0
        for entry in experience_raw:
            title = Agent.clean_str(
                Agent.first_of(entry, "title", "position", "role", "job_title", "name", default=""), 200
            )
            company = Agent.clean_str(
                Agent.first_of(
                    entry, "company", "company_name", "organisation", "organization",
                    "employer", "institution", "companyName", default="",
                ),
                200,
            )
            dates = Agent.clean_str(
                Agent.first_of(
                    entry, "dates", "date_range", "duration", "period", "timeframe",
                    "employment_period", "date", default="",
                ),
                200,
            )
            if not dates:
                start_txt = Agent.clean_str(
                    Agent.first_of(entry, "starts_at", "start_date", "start", "from", default=""), 60
                )
                end_txt = Agent.clean_str(
                    Agent.first_of(entry, "ends_at", "end_date", "end", "to", default=""), 60
                )
                dates = " - ".join(p for p in (start_txt, end_txt) if p)
            description = Agent.clean_str(
                Agent.first_of(
                    entry, "description", "summary", "details", "responsibilities",
                    "highlights", "bullets", default="",
                ),
                2400,
            )
            location = Agent.clean_str(
                Agent.first_of(entry, "location", "geo", "city", "place", default=""), 160
            )
            interval = _entry_interval(entry, now_ym)
            if interval:
                intervals.append(interval)
            else:
                duration_fallback += _duration_months(dates)
            if title or company or description:
                experience.append(
                    {
                        "title": title,
                        "company": company,
                        "location": location,
                        "dates": dates,
                        "description": description,
                    }
                )

        education: list[dict[str, Any]] = []
        for entry in education_raw:
            education.append(
                {
                    "school": Agent.clean_str(
                        Agent.first_of(
                            entry, "school", "institution", "university", "college",
                            "name", "school_name", default="",
                        ),
                        200,
                    ),
                    "degree": Agent.clean_str(
                        Agent.first_of(
                            entry, "degree", "degree_name", "qualification", "program",
                            "field_of_study", "title", default="",
                        ),
                        200,
                    ),
                    "dates": Agent.clean_str(
                        Agent.first_of(
                            entry, "dates", "date_range", "duration", "years", "period", default=""
                        ),
                        120,
                    ),
                }
            )

        certifications = Agent.str_list(
            [
                Agent.first_of(entry, "name", "title", "certification", "authority", default="")
                or Agent.clean_str(entry, 200)
                for entry in cert_raw
            ],
            limit=40,
        )
        projects = [
            {
                "name": Agent.clean_str(Agent.first_of(entry, "name", "title", default=""), 200),
                "description": Agent.clean_str(
                    Agent.first_of(entry, "description", "summary", "details", default=""), 1200
                ),
            }
            for entry in project_raw
        ]

        skills = Agent.str_list(
            Agent.first_of(body, "skills", "skill", "top_skills", "skill_list", default=[]),
            limit=120,
            item_limit=120,
        )

        months = _merged_months(intervals) if intervals else 0
        months = max(months, duration_fallback)
        years = round(months / 12.0, 1)

        titles = [e["title"] for e in experience if e["title"]]
        about = Agent.clean_str(
            Agent.first_of(body, "summary", "about", "bio", "description", "about_section", default=""),
            6000,
        )
        headline = Agent.clean_str(
            Agent.first_of(body, "headline", "occupation", "title", "sub_title", default=""), 300
        )
        name = Agent.clean_str(
            Agent.first_of(body, "full_name", "name", "fullname", "display_name", default=""), 160
        ) or " ".join(
            p
            for p in (
                Agent.clean_str(Agent.first_of(body, "first_name", "firstname", default=""), 80),
                Agent.clean_str(Agent.first_of(body, "last_name", "lastname", default=""), 80),
            )
            if p
        )

        corpus = "\n".join(
            [
                headline,
                about,
                " ".join(titles),
                " ".join(e["company"] for e in experience),
                " ".join(e["description"] for e in experience),
                " ".join(skills),
                " ".join(certifications),
                " ".join(f"{p['name']} {p['description']}" for p in projects),
                " ".join(f"{e['degree']} {e['school']}" for e in education),
            ]
        )

        return {
            "name": name,
            "headline": headline,
            "about": about,
            "location": Agent.clean_str(
                Agent.first_of(body, "location", "geo_location", "city", "country", default=""), 200
            ),
            "experience": experience,
            "education": education,
            "certifications": certifications,
            "projects": projects,
            "skills": skills,
            "titles": titles,
            "years_experience": years,
            "corpus": corpus,
            "raw": body,
        }

    # ------------------------------------------------------ deterministic pass

    @staticmethod
    def _infer_seniority(titles: list[str], years: float) -> str:
        """Best-effort seniority from job titles, with a years-of-experience floor."""
        haystack = " ".join(titles).lower()
        for label, needles in _SENIORITY_RULES:
            if any(needle in haystack for needle in needles):
                # A single internship should not brand a 10-year career as "intern".
                if label in ("intern", "junior") and years >= 4:
                    continue
                return label
        if years >= 12:
            return "principal"
        if years >= 7:
            return "senior"
        if years >= 3:
            return "mid"
        if years > 0:
            return "junior"
        return ""

    def _deterministic_analysis(self, facts: dict[str, Any]) -> dict[str, Any]:
        """A complete, honest analysis built without the LLM."""
        keywords = [phrase for phrase, _weight in extract_keywords(facts["corpus"], top_n=45)]
        bank = Agent.str_list(
            list(facts["skills"]) + list(facts["certifications"]) + keywords, limit=45, item_limit=80
        )

        achievements: list[dict[str, Any]] = []
        for entry in facts["experience"]:
            for line in re.split(r"[\n•·]+|(?<=[.!?])\s+(?=[A-Z])", entry["description"] or ""):
                text = Agent.clean_str(line, 300)
                if len(text) < 25:
                    continue
                metric = _METRIC_RE.search(text)
                achievements.append({"text": text, "metric": metric.group(0) if metric else None})
                if len(achievements) >= 12:
                    break
            if len(achievements) >= 12:
                break

        gaps: list[str] = []
        if not facts["about"]:
            gaps.append("LinkedIn 'About' section is empty -- no positioning statement to work from.")
        if not any(e["description"] for e in facts["experience"]):
            gaps.append("No role descriptions were scraped, so achievements cannot be evidenced.")
        if not any(a["metric"] for a in achievements):
            gaps.append("No quantified outcomes found -- add numbers you can actually verify.")
        if not facts["certifications"]:
            gaps.append("No certifications listed.")
        if len(facts["skills"]) < 5:
            gaps.append("Fewer than five skills listed; recruiter keyword search will miss this profile.")

        titles = facts["titles"]
        return {
            "headline": facts["headline"] or (titles[0] if titles else ""),
            "summary": facts["about"][:1200],
            "seniority": self._infer_seniority(titles, facts["years_experience"]),
            "years_experience": float(facts["years_experience"]),
            "domains": [],
            "hard_skills": Agent.str_list(facts["skills"], limit=25, item_limit=80),
            "soft_skills": [],
            "tools": [],
            "certifications": list(facts["certifications"]),
            "achievements": achievements,
            "keyword_bank": bank,
            "gaps": gaps,
            "target_roles": Agent.str_list(titles, limit=6, item_limit=80),
            "positioning_statement": "",
            "evidence": {
                "companies": Agent.str_list(
                    [e["company"] for e in facts["experience"]], limit=40, item_limit=120
                ),
                "titles": Agent.str_list(titles, limit=40, item_limit=120),
                "schools": Agent.str_list(
                    [e["school"] for e in facts["education"]], limit=20, item_limit=120
                ),
                "date_ranges": Agent.str_list(
                    [e["dates"] for e in facts["experience"]], limit=40, item_limit=80
                ),
            },
        }

    # ------------------------------------------------------------- LLM prompt

    def _build_prompt(self, facts: dict[str, Any]) -> str:
        """A readable digest beats raw JSON for small free-tier models."""
        lines: list[str] = ["LINKEDIN PROFILE (scraped; sections may be missing)", ""]
        if facts["name"]:
            lines.append(f"Name: {facts['name']}")
        if facts["headline"]:
            lines.append(f"Headline: {facts['headline']}")
        if facts["location"]:
            lines.append(f"Location: {facts['location']}")
        lines.append(
            f"Total tenure computed from the listed date ranges: {facts['years_experience']} years"
        )
        if facts["about"]:
            lines += ["", "ABOUT:", Agent.truncate(facts["about"], 2500)]

        if facts["experience"]:
            lines += ["", "EXPERIENCE (most recent first as scraped):"]
            for idx, entry in enumerate(facts["experience"][:15], 1):
                header = Agent.join_nonempty(
                    [entry["title"], entry["company"], entry["location"], entry["dates"]]
                )
                lines.append(f"{idx}. {header or '(unlabelled role)'}")
                if entry["description"]:
                    lines.append(f"   {Agent.truncate(entry['description'], 900)}")
        else:
            lines += ["", "EXPERIENCE: (none scraped)"]

        if facts["education"]:
            lines += ["", "EDUCATION:"]
            for entry in facts["education"][:8]:
                lines.append(
                    "- " + (Agent.join_nonempty([entry["degree"], entry["school"], entry["dates"]])
                            or "(unlabelled)")
                )
        if facts["certifications"]:
            lines += ["", "CERTIFICATIONS: " + ", ".join(facts["certifications"][:30])]
        if facts["projects"]:
            lines += ["", "PROJECTS:"]
            for project in facts["projects"][:8]:
                lines.append(
                    "- " + Agent.join_nonempty([project["name"], Agent.truncate(project["description"], 400)])
                )
        if facts["skills"]:
            lines += ["", "SKILLS LISTED: " + ", ".join(facts["skills"][:80])]

        digest = Agent.truncate("\n".join(lines), self.MAX_DIGEST_CHARS)

        tail = (
            "\n\nIf a section above is missing it was NOT scraped -- do not fill it in from "
            "imagination.\n"
            "Raw scrape (for keys the digest may have missed):\n"
            + Agent.compact_json(facts["raw"], self.MAX_RAW_JSON_CHARS)
            + "\n\nReturn ONLY this JSON object:\n"
            + self.SCHEMA_HINT
        )
        return digest + tail

    # ------------------------------------------------------------------ merge

    @staticmethod
    def _achievements(value: Any, limit: int = 14) -> list[dict[str, Any]]:
        """Normalise the model's achievements into ``[{text, metric}]``."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in Agent.as_list(value):
            if isinstance(item, dict):
                text = Agent.clean_str(
                    Agent.first_of(item, "text", "achievement", "description", "title", default=""), 400
                )
                metric = Agent.clean_str(
                    Agent.first_of(item, "metric", "measure", "impact", "result", default=""), 80
                )
            else:
                text = Agent.clean_str(item, 400)
                metric = ""
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            if not metric:
                found = _METRIC_RE.search(text)
                metric = found.group(0) if found else ""
            # Only keep a metric the achievement text actually contains: a model
            # that "summarised" a number into existence must not be believed.
            if metric and metric.lower() not in text.lower():
                metric = ""
            out.append({"text": text, "metric": metric or None})
            if len(out) >= limit:
                break
        return out

    def _merge(
        self, base: dict[str, Any], data: dict[str, Any], facts: dict[str, Any]
    ) -> dict[str, Any]:
        """Overlay the model's judgement on the deterministic facts."""
        merged = dict(base)
        corpus_low = facts["corpus"].lower()

        def _text(key: str, limit: int, fallback: str) -> str:
            value = Agent.clean_str(Agent.first_of(data, key, default=""), limit)
            return value or fallback

        merged["headline"] = _text("headline", 200, base["headline"])
        merged["summary"] = _text("summary", 2000, base["summary"])
        merged["positioning_statement"] = _text("positioning_statement", 400, "")

        seniority = Agent.clean_str(Agent.first_of(data, "seniority", "level", default=""), 40).lower()
        valid = {"intern", "junior", "mid", "senior", "principal", "manager", "director", "executive"}
        merged["seniority"] = seniority if seniority in valid else base["seniority"]

        # Tenure computed from real date ranges always wins; the model only fills
        # the gap when nothing datable was scraped.
        if base["years_experience"] > 0:
            merged["years_experience"] = base["years_experience"]
        else:
            merged["years_experience"] = round(
                Agent.clamp(Agent.first_of(data, "years_experience", "years", default=0), 0.0, 60.0, 0.0), 1
            )

        merged["domains"] = Agent.str_list(
            Agent.first_of(data, "domains", "industries", default=[]), limit=8, item_limit=80
        )
        merged["hard_skills"] = Agent.str_list(
            list(Agent.as_list(Agent.first_of(data, "hard_skills", "technical_skills", default=[])))
            + base["hard_skills"],
            limit=25,
            item_limit=80,
        )
        merged["soft_skills"] = Agent.str_list(
            Agent.first_of(data, "soft_skills", default=[]), limit=10, item_limit=80
        )
        merged["tools"] = Agent.str_list(
            Agent.first_of(data, "tools", "technologies", "platforms", default=[]),
            limit=30,
            item_limit=80,
        )

        # Certifications are a credential claim: accept a model entry only when the
        # scrape actually mentions it.
        model_certs = [
            cert
            for cert in Agent.str_list(
                Agent.first_of(data, "certifications", "certs", default=[]), limit=40, item_limit=160
            )
            if cert.lower() in corpus_low
        ]
        merged["certifications"] = Agent.str_list(
            base["certifications"] + model_certs, limit=40, item_limit=160
        )

        merged["achievements"] = (
            self._achievements(Agent.first_of(data, "achievements", "accomplishments", default=[]))
            or base["achievements"]
        )
        merged["keyword_bank"] = Agent.str_list(
            list(Agent.as_list(Agent.first_of(data, "keyword_bank", "keywords", default=[])))
            + base["keyword_bank"],
            limit=45,
            item_limit=80,
        )
        merged["gaps"] = Agent.str_list(
            list(Agent.as_list(Agent.first_of(data, "gaps", "weaknesses", default=[]))) + base["gaps"],
            limit=10,
            item_limit=300,
        )
        merged["target_roles"] = Agent.str_list(
            list(Agent.as_list(Agent.first_of(data, "target_roles", "roles", default=[])))
            + base["target_roles"],
            limit=6,
            item_limit=80,
        )
        return merged


def extract_profile_facts(raw_profile: Any) -> dict[str, Any]:
    """Unwrap + extract a raw LinkedIn scrape into provable facts (no LLM, no IO).

    Returns ``{"name","headline","about","location","experience","education",
    "certifications","projects","skills","titles","years_experience","corpus","raw"}``.
    ``ResumeArchitect`` uses this as its evidence base for the anti-fabrication check.
    """
    return ProfileAnalyst.extract_facts(_unwrap(raw_profile))


def empty_analysis() -> dict[str, Any]:
    """A contract-shaped analysis with no content (used by callers as a default)."""
    return {
        "headline": "",
        "summary": "",
        "seniority": "",
        "years_experience": 0.0,
        "domains": [],
        "hard_skills": [],
        "soft_skills": [],
        "tools": [],
        "certifications": [],
        "achievements": [],
        "keyword_bank": [],
        "gaps": [],
        "target_roles": [],
        "positioning_statement": "",
    }
