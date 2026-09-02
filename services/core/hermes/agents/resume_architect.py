"""ResumeArchitect -- write an ATS-safe, single-column resume in Markdown.

The output of :meth:`ResumeArchitect.build` is the canonical Hermes resume
artefact: ``hermes.render.render_resume`` converts the markdown into .docx/.pdf/
.txt via ``services/sandbox/ats_docx.py``, and ``hermes.agents.ats`` scores that
same markdown. The three modules therefore agree on one structure -- the
canonical section list lives in :data:`hermes.agents.ats.CANONICAL_SECTIONS` and
is imported here rather than restated.

Markdown contract (what the renderer expects, and what this agent emits)::

    # Candidate Name
    Location | email | phone | linkedin.com/in/slug

    ## PROFESSIONAL SUMMARY
    Plain prose, no pronouns.

    ## CORE COMPETENCIES
    - Competency, Competency, Competency

    ## PROFESSIONAL EXPERIENCE
    **Job Title — Company | Location | Mon YYYY - Mon YYYY**
    - Past-tense action verb + what + measurable outcome.

Single column, no tables, no graphics, no text boxes, no headers/footers, no
columns simulated with spaces or tabs -- every one of those is a documented
failure mode of mainstream resume parsers.

ANTI-FABRICATION
----------------
The system prompt forbids inventing employers, titles, dates, degrees,
certifications and metrics -- and because a prompt is not an enforcement
mechanism, :meth:`ResumeArchitect.build` re-reads its own output afterwards and
flags every organisation name, year and metric in the generated document that
does not appear in the source material (the LinkedIn scrape, the analysis, or an
uploaded base resume). Those flags are returned in ``rationale`` (and, structured,
in ``flags``) so the dashboard can show them next to the resume. Hermes does not
silently "fix" them: a human has to decide whether the fact is real.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from hermes.agents.ats import (
    ACTION_VERBS,
    CANONICAL_SECTIONS,
    OPTIONAL_SECTIONS,
    REQUIRED_SECTIONS,
    collect_bullets,
    extract_keywords,
    normalize_token,
    split_sections,
    stem,
    tokenize,
)
from hermes.agents.base import Agent
from hermes.agents.profile_analyst import extract_profile_facts

log = logging.getLogger(__name__)

__all__ = ["ResumeArchitect"]

#: Stemmed action verbs, so "Automating"/"Automated"/"Automate" all count as one.
_ACTION_VERB_STEMS: frozenset[str] = frozenset(stem(verb) for verb in ACTION_VERBS)


# --------------------------------------------------------------------------- #
# Cleanup patterns
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$|^\s*~~~[a-zA-Z0-9_+-]*\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{6,}\|?\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(r"</?(?:div|span|table|tr|td|th|p|br|font|u|hr|header|footer)\b[^>]*>", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REFERENCES_RE = re.compile(r"^\s*[-*+]?\s*references?\s+(?:available|upon|on)\b.*$", re.IGNORECASE)
_BULLET_CHAR_RE = re.compile(r"^(\s{0,8})[*+•▪●·⁃‣◦∙>]\s+")
_PREAMBLE_RE = re.compile(
    r"^\s*(?:here(?:'s| is)|below is|sure|certainly|i(?:'ve| have)|as requested|"
    r"the following is|this is)\b[^\n]{0,160}$",
    re.IGNORECASE,
)
_MULTISPACE_RE = re.compile(r"(\S)[ \t]{3,}(\S)")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\w/])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{3,5}(?:[\s.\-]\d{2,5}){1,3}(?![\w/])"
)
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[A-Za-z0-9\-_%.]+", re.IGNORECASE
)

#: Deliberately \b-anchored (not lookaround-anchored) so that "2019-2022" in the
#: source profile registers BOTH years as allowed. Asymmetry here would produce
#: false fabrication flags on perfectly honest dates.
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

#: First-person pronouns. "I" is excluded when it is a numeral ("Phase I").
_PRONOUN_RE = re.compile(r"\b(?:my|me|mine|myself|we|our|ours|us)\b", re.IGNORECASE)
_BARE_I_RE = re.compile(r"(?<!\w)I(?:'m|'ve|'ll|'d)?\s+(?=[a-z])")
_NUMERAL_I_CONTEXT_RE = re.compile(
    r"\b(?:phase|level|type|class|part|tier|group|stage|grade|appendix|annex|volume|section)\s+I\b",
    re.IGNORECASE,
)
_METRIC_RE = re.compile(
    r"\d+(?:[.,]\d+)*\s*%"
    r"|[$€£₹¥]\s?\d+(?:[.,]\d+)*\s*(?:[KMB]|k|m|bn|billion|million|thousand|lakh|crore)?"
    r"|\b\d+(?:[.,]\d+)*\s*(?:[KMB]\b|x\b|bps\b|FTEs?\b|hrs?\b|hours?\b|days?\b|weeks?\b|months?\b)"
    r"|\b\d{2,}(?:[.,]\d{3})+\b",
    re.IGNORECASE,
)

#: Segments of an experience/education header line that are never organisations.
_NON_ORG_SEGMENTS: frozenset[str] = frozenset(
    {
        "present", "current", "currently", "ongoing", "to date", "till date", "now",
        "remote", "hybrid", "on site", "on-site", "onsite", "full time", "full-time",
        "part time", "part-time", "contract", "contractor", "freelance", "temporary",
        "permanent", "internship", "intern", "consultant", "self employed",
        "self-employed", "india", "usa", "uk", "united states", "united kingdom",
        "promoted", "promotion", "various clients", "multiple clients", "confidential",
    }
)

#: Splitters used to break a role/education header into candidate segments.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\||—|–|·|•|,|/|\bat\b|•)\s*", re.IGNORECASE)

#: Cap on how many flags we surface; a wall of warnings is ignored, not read.
_MAX_FLAGS = 14


def _has_pronouns(text: str) -> bool:
    """True when the document uses first-person pronouns (an ATS/style defect)."""
    body = _NUMERAL_I_CONTEXT_RE.sub(" ", text or "")
    return bool(_PRONOUN_RE.search(body) or _BARE_I_RE.search(body))


def _first_word(text: str) -> str:
    """The first alphabetic word of a bullet, ignoring markdown decoration."""
    cleaned = re.sub(r"^[^A-Za-z]+", "", (text or "").replace("*", "").replace("_", ""))
    match = re.match(r"[A-Za-z][A-Za-z'\-]*", cleaned)
    return match.group(0).lower() if match else ""


def _normalise_for_match(text: str) -> str:
    """Lowercase, strip punctuation and collapse spaces -- for substring evidence checks."""
    lowered = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _stem_set(text: str) -> tuple[set[str], set[str]]:
    """(unigram stems, bigram stems) of ``text`` -- mirrors the ATS matcher."""
    unigrams: set[str] = set()
    bigrams: set[str] = set()
    previous: str | None = None
    for token in tokenize(text or ""):
        norm = normalize_token(token)
        if not norm:
            previous = None
            continue
        unigrams.add(norm)
        if previous:
            bigrams.add(f"{previous} {norm}")
        previous = norm
    return unigrams, bigrams


class ResumeArchitect(Agent):
    """Generate (and then audit) an ATS-safe resume for a profile / target job."""

    NAME = "resume-architect"

    SYSTEM = """You are an elite resume writer who has spent a decade reverse-engineering how
Applicant Tracking Systems parse documents and how recruiters skim them. You produce ONE
Markdown resume. No commentary, no explanation, no code fences -- the document only.

FORMAT RULES (these are ATS parsing requirements, not preferences)
1. Strict single column. Never use tables, grids, columns, text boxes, headers, footers,
   page numbers, images, logos, icons, emoji, charts or horizontal rules. Never simulate
   columns with multiple spaces or tabs.
2. Start with `# Full Name`, then ONE plain line of contact details separated by ` | `
   (location, email, phone, linkedin.com/in/... ). Include only details supplied to you.
3. Use `## SECTION NAME` in capitals, and only these headings, in exactly this order:
   PROFESSIONAL SUMMARY, CORE COMPETENCIES, PROFESSIONAL EXPERIENCE, PROJECTS, EDUCATION,
   CERTIFICATIONS, TECHNICAL SKILLS. Omit a section entirely if there is no real content
   for it -- an empty heading is worse than no heading.
4. PROFESSIONAL EXPERIENCE and EDUCATION are reverse-chronological (most recent first).
5. Every role is introduced by exactly one bold line:
   `**Job Title — Company | Location | Mon YYYY - Mon YYYY**`
   Use the three-letter month form and the same format for every date in the document.
   Use `Present` for a current role. No other date style anywhere.
6. Bullets start with `- ` and nothing else. 3-6 bullets per role, 12-28 words each.
7. Every bullet opens with a strong PAST-TENSE action verb (Led, Automated, Reduced,
   Migrated, Negotiated...). Never open with "Responsible for", "Worked on", "Helped with",
   "Involved in", or a noun phrase. Present tense is allowed only for the current role's
   ongoing duties, and even then prefer past tense.
8. Quantify impact wherever a number exists in the source material (%, currency, volume,
   headcount, time saved, scale). If no number is available, write the achievement plainly
   and completely -- an unquantified true bullet beats an invented number.
9. No first- or second-person pronouns (I, me, my, we, our, you). No "References available
   upon request". No photo, no marital status, no date of birth, no salary.
10. CORE COMPETENCIES is 3-5 bullet lines of comma-separated capability phrases.
    TECHNICAL SKILLS is grouped bullets, e.g. `- Cloud: AWS, Azure, Terraform`.
11. Plain ASCII punctuation. The only non-ASCII character allowed is the em dash in a role
    header line.

TAILORING RULES
- Mirror the vocabulary of the target job description: if it says "observability" use
  "observability", not "monitoring". Match its exact tool and standard names.
- Weave those terms into real accomplishments ONLY where they are true for this candidate.
  Never list a skill the candidate has no evidence for. Do not repeat a keyword more than
  three or four times -- keyword stuffing is detected and penalised by modern parsers and
  is obvious to a human reader.
- Lead with the experience the target role cares about; compress unrelated roles to one or
  two bullets rather than deleting them (gaps in dates raise questions).

ABSOLUTE ANTI-FABRICATION RULE
- You may ONLY use employers, job titles, employment dates, locations, schools, degrees,
  graduation dates, certifications, tools and metrics that appear in the source material
  supplied below. You may rewrite, compress, re-order, re-word and re-frame that material
  freely -- you may NOT add to it.
- Never invent a company, a title, a date range, a degree, a certification, a client name,
  a team size, a budget, a percentage or any other number. If a metric is unknown, write
  the achievement WITHOUT a number.
- Never present the target employer as a past employer.
- If the source material is thin, produce a shorter resume. A short true resume is the
  correct output; a padded invented one is a failure.

Output the Markdown document and nothing else."""

    #: Prompt budgets (free-tier context windows are small).
    MAX_JD_CHARS = 6000
    MAX_BASE_RESUME_CHARS = 9000
    MAX_FACTS_CHARS = 9000
    MAX_TOKENS = 3400

    async def build(
        self,
        analysis: dict,
        profile_raw: dict,
        target_job: dict | None = None,
        base_resume_text: str | None = None,
    ) -> dict:
        """Write one resume.

        Parameters
        ----------
        analysis:
            ``ProfileAnalyst.analyze()`` output (may be sparse).
        profile_raw:
            The raw LinkedIn scrape; used as the evidence base for the
            anti-fabrication audit and for facts the analysis flattened away.
        target_job:
            Optional job payload (``routes._common.job_payload`` shape). When it
            carries a ``match_breakdown`` from ``MatchRanker``, its
            ``tailoring_notes`` are fed to the model as explicit instructions.
        base_resume_text:
            Optional resume the user uploaded. Treated as *additional source
            material* (it usually holds the contact details and metrics LinkedIn
            never exposes) -- never as a template to copy verbatim.

        Returns
        -------
        ``{"markdown", "sections", "keywords_used", "rationale", "flags", "checks"}``
        """
        analysis = analysis if isinstance(analysis, dict) else {}
        facts = extract_profile_facts(profile_raw if isinstance(profile_raw, dict) else {})
        base_text = Agent.clean_str(base_resume_text or "", None)
        contact = self._contact_details(facts, base_text)
        job = target_job if isinstance(target_job, dict) else None

        jd_text = Agent.clean_str((job or {}).get("description") or "", None)
        jd_keywords = [phrase for phrase, _w in extract_keywords(jd_text, top_n=32)] if jd_text else []
        notes = self._tailoring_notes(job)

        if job:
            await self.emit(
                f"Tailoring for '{job.get('title') or 'role'}' @ "
                f"{job.get('company') or 'unknown company'} — "
                f"{len(jd_keywords)} JD keyword(s), {len(notes)} tailoring note(s)."
                + ("" if jd_text else " No job description was scraped, so tailoring uses the title only.")
            )
        else:
            await self.emit("Building a general-purpose base resume (no target job).")

        if not facts["experience"] and not base_text:
            await self.emit(
                "No work history in the profile and no uploaded resume — the generated resume "
                "will be very thin. Import your LinkedIn profile or upload a base resume.",
                "warn",
            )

        prompt = self._build_prompt(analysis, facts, contact, job, jd_text, jd_keywords, notes, base_text)

        await self.emit("Drafting the resume…")
        raw = await self.ask(prompt, temperature=0.25, max_tokens=self.MAX_TOKENS)
        markdown = self._clean(raw)
        sections = self._sections(markdown)

        problems = self._structural_problems(markdown, sections)
        if problems:
            await self.emit(
                "Draft failed the structural check (" + "; ".join(problems[:3]) + "). Requesting one revision.",
                "warn",
            )
            try:
                repaired = await self.ask(
                    self._repair_prompt(markdown, problems),
                    temperature=0.1,
                    max_tokens=self.MAX_TOKENS,
                )
                candidate = self._clean(repaired)
                candidate_sections = self._sections(candidate)
                if len(self._structural_problems(candidate, candidate_sections)) < len(problems):
                    markdown, sections = candidate, candidate_sections
                    problems = self._structural_problems(markdown, sections)
            except Exception as exc:  # a failed revision must not lose the draft
                log.warning("ResumeArchitect revision pass failed: %s", exc)
                await self.emit(f"Revision pass failed ({type(exc).__name__}); keeping the first draft.", "warn")

        if not markdown.strip():
            raise RuntimeError(
                "The model returned no resume text. Check the freellmapi router "
                "(GET /api/llm/models) and try a different HERMES_MODEL_PRIMARY."
            )

        keywords_used, keywords_missing = self._keyword_audit(markdown, jd_keywords, analysis)
        flags = self._fabrication_flags(markdown, sections, facts, analysis, base_text, job)
        checks = self._checks(markdown, sections, contact, problems)
        rationale = self._rationale(
            job, jd_keywords, keywords_used, keywords_missing, notes, flags, checks, contact, problems
        )

        level = "warn" if flags else "info"
        await self.emit(
            f"Resume drafted — {checks['word_count']} words, {len(sections)} section(s), "
            f"{checks['bullet_count']} bullets, {len(keywords_used)} target keyword(s) used"
            + (f", {len(flags)} fact(s) flagged for review." if flags else "."),
            level,
        )
        for flag in flags[:5]:
            await self.emit(flag, "warn")

        return {
            "markdown": markdown,
            "sections": sections,
            "keywords_used": keywords_used,
            "rationale": rationale,
            "flags": flags,
            "checks": checks,
        }

    # ------------------------------------------------------------------ inputs

    @staticmethod
    def _contact_details(facts: dict[str, Any], base_text: str) -> dict[str, str]:
        """Contact details we can prove, from the scrape and the uploaded resume.

        LinkedIn almost never exposes an email or a phone number, so an uploaded
        base resume is usually the only place they exist. Nothing is invented: a
        missing detail is reported so the user can add it.
        """
        raw_blob = Agent.compact_json(facts.get("raw") or {}, 20000)
        haystacks = [base_text, raw_blob, facts.get("about") or "", facts.get("headline") or ""]
        email = phone = linkedin = ""
        for text in haystacks:
            if not text:
                continue
            if not email:
                match = _EMAIL_RE.search(text)
                email = match.group(0) if match else ""
            if not linkedin:
                match = _LINKEDIN_RE.search(text)
                linkedin = match.group(0) if match else ""
            if not phone:
                for match in _PHONE_RE.finditer(text):
                    digits = re.sub(r"\D", "", match.group(0))
                    if 9 <= len(digits) <= 15 and not re.fullmatch(r"(?:19|20)\d{2}\d{4}", digits):
                        phone = match.group(0).strip()
                        break
        name = facts.get("name") or ""
        if not name and base_text:
            first_line = next((ln.strip() for ln in base_text.splitlines() if ln.strip()), "")
            if first_line and len(first_line) <= 60 and not _EMAIL_RE.search(first_line):
                name = re.sub(r"^#+\s*", "", first_line)
        return {
            "name": Agent.clean_str(name, 120),
            "location": Agent.clean_str(facts.get("location") or "", 120),
            "email": email,
            "phone": phone,
            "linkedin": linkedin,
        }

    @staticmethod
    def _tailoring_notes(job: dict[str, Any] | None) -> list[str]:
        """MatchRanker's per-job instructions, if this job has been ranked."""
        if not job:
            return []
        breakdown = job.get("match_breakdown")
        if not isinstance(breakdown, dict):
            return []
        notes = Agent.str_list(breakdown.get("tailoring_notes"), limit=10, item_limit=400)
        missing = Agent.str_list(breakdown.get("missing_skills"), limit=12, item_limit=80)
        if missing:
            notes.append(
                "Job description terms currently absent from the profile — use them ONLY where "
                "they are genuinely true for this candidate: " + ", ".join(missing)
            )
        return notes

    def _build_prompt(
        self,
        analysis: dict[str, Any],
        facts: dict[str, Any],
        contact: dict[str, str],
        job: dict[str, Any] | None,
        jd_text: str,
        jd_keywords: list[str],
        notes: list[str],
        base_text: str,
    ) -> str:
        parts: list[str] = []

        header = ["CANDIDATE IDENTITY (use exactly these, omit anything blank):"]
        header.append(f"- Name: {contact['name'] or '(unknown — use the name from the source resume if present)'}")
        header.append(f"- Location: {contact['location'] or '(not supplied — omit the location)'}")
        header.append(f"- Email: {contact['email'] or '(not supplied — omit the email)'}")
        header.append(f"- Phone: {contact['phone'] or '(not supplied — omit the phone)'}")
        header.append(f"- LinkedIn: {contact['linkedin'] or '(not supplied — omit the LinkedIn line)'}")
        parts.append("\n".join(header))

        analysis_block = [
            "PROFILE ANALYSIS:",
            f"- Headline: {Agent.clean_str(analysis.get('headline'), 200)}",
            f"- Seniority: {Agent.clean_str(analysis.get('seniority'), 40)}",
            f"- Years of experience: {analysis.get('years_experience') or facts.get('years_experience') or 0}",
            f"- Positioning: {Agent.clean_str(analysis.get('positioning_statement'), 400)}",
            f"- Domains: {', '.join(Agent.str_list(analysis.get('domains'), limit=10))}",
            f"- Hard skills: {', '.join(Agent.str_list(analysis.get('hard_skills'), limit=30))}",
            f"- Tools: {', '.join(Agent.str_list(analysis.get('tools'), limit=30))}",
            f"- Soft skills: {', '.join(Agent.str_list(analysis.get('soft_skills'), limit=12))}",
            f"- Certifications: {', '.join(Agent.str_list(analysis.get('certifications'), limit=20))}",
            f"- Keyword bank: {', '.join(Agent.str_list(analysis.get('keyword_bank'), limit=45))}",
        ]
        achievements = analysis.get("achievements")
        if isinstance(achievements, list) and achievements:
            analysis_block.append("- Stated achievements (metrics are verbatim; do not change them):")
            for item in achievements[:14]:
                if isinstance(item, dict):
                    text = Agent.clean_str(item.get("text"), 300)
                    metric = Agent.clean_str(item.get("metric"), 60)
                else:
                    text, metric = Agent.clean_str(item, 300), ""
                if text:
                    analysis_block.append(f"  * {text}" + (f"  [metric: {metric}]" if metric else ""))
        parts.append("\n".join(analysis_block))

        parts.append(Agent.truncate(self._facts_block(facts), self.MAX_FACTS_CHARS))

        if base_text:
            parts.append(
                "UPLOADED BASE RESUME (additional source material — reuse its facts, metrics and "
                "contact details; do NOT copy its layout or its section names):\n"
                + Agent.truncate(base_text, self.MAX_BASE_RESUME_CHARS)
            )

        if job:
            job_block = [
                "TARGET JOB:",
                f"- Title: {Agent.clean_str(job.get('title'), 200)}",
                f"- Company: {Agent.clean_str(job.get('company'), 200)}",
                f"- Location: {Agent.clean_str(job.get('location'), 200)}",
            ]
            if jd_keywords:
                job_block.append("- Highest-weighted JD terms: " + ", ".join(jd_keywords))
            if jd_text:
                job_block.append(
                    "- Job description:\n" + Agent.truncate(jd_text, self.MAX_JD_CHARS)
                )
            else:
                job_block.append(
                    "- Job description: NOT AVAILABLE. Tailor using the title and company only; "
                    "do not guess at requirements."
                )
            parts.append("\n".join(job_block))
            company = Agent.clean_str(job.get("company"), 200)
            parts.append(
                f"IMPORTANT: '{company}' is the TARGET employer, not a past one. It must never "
                "appear in PROFESSIONAL EXPERIENCE."
                if company
                else "IMPORTANT: the target employer must never appear in PROFESSIONAL EXPERIENCE."
            )
        if notes:
            parts.append(
                "TAILORING INSTRUCTIONS (from the match analysis for this specific job):\n"
                + Agent.bullet_lines(notes)
            )

        parts.append(
            "TASK: Write the complete resume in Markdown, following every FORMAT, TAILORING and "
            "ANTI-FABRICATION rule. Sections, in order, omitting any with no real content: "
            + ", ".join(CANONICAL_SECTIONS)
            + ".\nStrong verbs to draw on: "
            + ", ".join(sorted(list(ACTION_VERBS))[:60])
            + ".\nOutput ONLY the Markdown document — no preamble, no code fence, no closing remarks."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _facts_block(facts: dict[str, Any]) -> str:
        """The verifiable source material, rendered for the prompt."""
        lines = ["SOURCE PROFILE (the ONLY employers, titles, dates and schools you may use):"]
        if facts.get("about"):
            lines += ["", "About: " + Agent.truncate(facts["about"], 1800)]
        experience = facts.get("experience") or []
        lines += ["", "Roles (as scraped, most recent first):"]
        if experience:
            for idx, entry in enumerate(experience[:15], 1):
                lines.append(
                    f"{idx}. Title: {entry.get('title') or '(none)'} | "
                    f"Company: {entry.get('company') or '(none)'} | "
                    f"Location: {entry.get('location') or '(none)'} | "
                    f"Dates: {entry.get('dates') or '(none)'}"
                )
                if entry.get("description"):
                    lines.append("   Source detail: " + Agent.truncate(entry["description"], 1000))
        else:
            lines.append("(no roles were scraped)")

        education = facts.get("education") or []
        lines += ["", "Education:"]
        if education:
            for entry in education[:8]:
                lines.append(
                    f"- Degree: {entry.get('degree') or '(none)'} | "
                    f"School: {entry.get('school') or '(none)'} | "
                    f"Dates: {entry.get('dates') or '(none)'}"
                )
        else:
            lines.append("(no education was scraped)")

        if facts.get("certifications"):
            lines += ["", "Certifications: " + ", ".join(facts["certifications"][:30])]
        projects = facts.get("projects") or []
        if projects:
            lines += ["", "Projects:"]
            for project in projects[:8]:
                lines.append(
                    "- "
                    + Agent.join_nonempty(
                        [project.get("name"), Agent.truncate(project.get("description") or "", 400)]
                    )
                )
        if facts.get("skills"):
            lines += ["", "Skills listed on the profile: " + ", ".join(facts["skills"][:80])]
        return "\n".join(lines)

    def _repair_prompt(self, markdown: str, problems: list[str]) -> str:
        return (
            "The resume below violates these rules:\n"
            + Agent.bullet_lines(problems)
            + "\n\nRewrite it so every violation is fixed. Keep every factual claim exactly as it "
            "is — do not add employers, dates, certifications or numbers, and do not remove real "
            "content to satisfy a rule. Required section order: "
            + ", ".join(CANONICAL_SECTIONS)
            + ".\nOutput ONLY the corrected Markdown document.\n\n---\n"
            + Agent.truncate(markdown, 14000)
        )

    # ---------------------------------------------------------------- cleanup

    @staticmethod
    def _clean(raw: str) -> str:
        """Strip model scaffolding and anything that is not ATS-safe markdown."""
        text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        text = _HTML_COMMENT_RE.sub("", text)
        text = _IMAGE_RE.sub("", text)
        text = _HTML_RE.sub("", text)

        out: list[str] = []
        seen_content = False
        for line in text.split("\n"):
            if _FENCE_RE.match(line):
                continue
            stripped = line.strip()
            if not seen_content:
                # Drop chatty preambles before the document actually starts.
                if not stripped:
                    continue
                if _PREAMBLE_RE.match(stripped):
                    continue
                seen_content = True
            if _TABLE_SEP_RE.match(line):
                continue
            if _TABLE_ROW_RE.match(line):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                cells = [c for c in cells if c and not set(c) <= set("-: ")]
                if cells:
                    out.append("- " + ", ".join(cells))
                continue
            if _REFERENCES_RE.match(line):
                continue
            if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
                continue  # horizontal rule
            line = _BULLET_CHAR_RE.sub(r"\1- ", line)
            line = line.replace("\t", " ")
            line = _MULTISPACE_RE.sub(r"\1 \2", line)
            out.append(line.rstrip())

        text = "\n".join(out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Trailing model chatter ("Let me know if...") after the last section.
        text = re.sub(
            r"\n+(?:let me know|i hope this|feel free to|note:|disclaimer:)[^\n]*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return text.strip() + "\n"

    @staticmethod
    def _sections(markdown: str) -> dict[str, str]:
        """Canonical sections present in the document, in canonical order."""
        split = split_sections(markdown)
        out: dict[str, str] = {}
        header = (split.get("HEADER") or "").strip()
        if header:
            out["HEADER"] = header
        for name in CANONICAL_SECTIONS:
            body = (split.get(name) or "").strip()
            if body:
                out[name] = body
        return out

    # --------------------------------------------------------------- auditing

    @staticmethod
    def _structural_problems(markdown: str, sections: dict[str, str]) -> list[str]:
        """Hard, mechanical violations worth one revision round-trip."""
        problems: list[str] = []
        text = markdown or ""
        words = len(re.findall(r"\b[\w'\-]+\b", text))

        missing = [name for name in REQUIRED_SECTIONS if name not in sections]
        if missing:
            problems.append(
                "missing required section heading(s): " + ", ".join(missing)
                + " (use the exact '## NAME' wording)"
            )
        if not text.lstrip().startswith("#"):
            problems.append("the document must open with '# Full Name'")
        if words < 250:
            problems.append(f"the resume is only {words} words; expand real content toward 400-900 words")
        if words > 1400:
            problems.append(f"the resume is {words} words; cut filler down to 400-1000 words")

        bullets = collect_bullets(text, sections=sections)
        if not bullets:
            problems.append("PROFESSIONAL EXPERIENCE has no '- ' bullets")
        else:
            weak = [
                bullet for bullet in bullets
                if re.match(
                    r"^\s*(?:responsible for|worked on|helped|assisted with|involved in|"
                    r"duties includ|tasked with|participated in)",
                    bullet,
                    re.IGNORECASE,
                )
                or stem(_first_word(bullet)) not in _ACTION_VERB_STEMS
            ]
            if len(weak) > max(1, len(bullets) // 5):
                problems.append(
                    f"{len(weak)} of {len(bullets)} bullets do not open with a strong past-tense "
                    "action verb (no 'Responsible for', 'Worked on', 'Helped with')"
                )
        if _has_pronouns(text):
            problems.append("first-person pronouns are present and must be removed")
        if _TABLE_ROW_RE.search(text) or "<table" in text.lower():
            problems.append("the document still contains a table")
        # Section order (the renderer tolerates any order; recruiters do not).
        order = [name for name in sections if name != "HEADER"]
        expected = [name for name in CANONICAL_SECTIONS if name in sections]
        if order != expected:
            problems.append("sections are out of order; use " + ", ".join(expected))
        return problems

    def _keyword_audit(
        self, markdown: str, jd_keywords: list[str], analysis: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Which target keywords the document actually contains."""
        targets = list(jd_keywords)
        if not targets:
            targets = Agent.str_list(analysis.get("keyword_bank"), limit=40, item_limit=80)
        unigrams, bigrams = _stem_set(markdown)
        used: list[str] = []
        missing: list[str] = []
        for phrase in targets:
            parts = [normalize_token(p) for p in str(phrase).split() if normalize_token(p)]
            if not parts:
                continue
            if len(parts) == 1:
                (used if parts[0] in unigrams else missing).append(phrase)
            else:
                key = " ".join(parts)
                if key in bigrams or all(p in unigrams for p in parts):
                    used.append(phrase)
                else:
                    missing.append(phrase)
        return used, missing

    @staticmethod
    def _evidence_haystack(
        facts: dict[str, Any], analysis: dict[str, Any], base_text: str
    ) -> tuple[str, set[str]]:
        """Everything the resume is *allowed* to assert, plus the allowed year set."""
        chunks: list[str] = [
            facts.get("corpus") or "",
            base_text,
            Agent.compact_json(facts.get("raw") or {}, 24000),
            " ".join(Agent.str_list(analysis.get("keyword_bank"), limit=60)),
            " ".join(Agent.str_list(analysis.get("certifications"), limit=40)),
            " ".join(Agent.str_list(analysis.get("hard_skills"), limit=40)),
            " ".join(Agent.str_list(analysis.get("tools"), limit=60)),
            Agent.clean_str(analysis.get("summary"), None),
            Agent.clean_str(analysis.get("headline"), None),
        ]
        for entry in facts.get("experience") or []:
            chunks.append(" ".join(str(v) for v in entry.values() if v))
        for entry in facts.get("education") or []:
            chunks.append(" ".join(str(v) for v in entry.values() if v))
        source = "\n".join(c for c in chunks if c)
        years = set(_YEAR_RE.findall(source))
        return _normalise_for_match(source), years

    def _fabrication_flags(
        self,
        markdown: str,
        sections: dict[str, str],
        facts: dict[str, Any],
        analysis: dict[str, Any],
        base_text: str,
        job: dict[str, Any] | None,
    ) -> list[str]:
        """Flag organisations, dates and metrics that the source material does not support.

        This is a *review aid*, not a censor: Hermes never edits a flagged claim
        away, because the user may simply have facts LinkedIn never exposed.
        """
        haystack, allowed_years = self._evidence_haystack(facts, analysis, base_text)
        flags: list[str] = []

        # --- organisations / schools in header lines --------------------------
        header_sections = ("PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS", "PROJECTS")
        checked: set[str] = set()
        for name in header_sections:
            body = sections.get(name)
            if not body:
                continue
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("-") or stripped.startswith("#"):
                    continue
                plain = re.sub(r"[*_`]", "", stripped)
                for segment in _SEGMENT_SPLIT_RE.split(plain):
                    candidate = Agent.clean_str(segment, 120).strip(" .:-")
                    norm = _normalise_for_match(candidate)
                    if not norm or norm in checked:
                        continue
                    if len(norm) < 4 or norm in _NON_ORG_SEGMENTS:
                        continue
                    if not re.search(r"[A-Za-z]{3}", candidate):
                        continue
                    # Dates and role titles are audited separately.
                    if _YEAR_RE.search(candidate) or re.fullmatch(r"[\d\s\-–—/]+", candidate):
                        continue
                    checked.add(norm)
                    if norm in haystack:
                        continue
                    # A title built from ordinary resume vocabulary is not a
                    # fabricated organisation; only flag proper-noun-looking text.
                    if not re.search(r"\b[A-Z][a-zA-Z&.\-]{2,}", candidate):
                        continue
                    flags.append(
                        f"FLAG [{name}]: '{candidate}' does not appear anywhere in your LinkedIn "
                        "profile, analysis or uploaded resume. Verify it or remove it."
                    )
                    if len(flags) >= _MAX_FLAGS:
                        return flags

        # --- certification claims (these live in bullets, not header lines) ---
        for bullet in collect_bullets(sections.get("CERTIFICATIONS") or ""):
            claim = Agent.clean_str(re.sub(r"[*_`]", "", bullet), 240)
            if not claim:
                continue
            # Audit the credential name itself; a trailing issuer/date is fine.
            head = next((p for p in _SEGMENT_SPLIT_RE.split(claim) if p.strip()), claim)
            norm = _normalise_for_match(head)
            if len(norm) < 4 or norm in haystack:
                continue
            # Acronyms in parentheses ("... (CISA)") often carry the only match.
            acronyms = re.findall(r"\(([A-Za-z0-9\-]{2,12})\)", claim)
            if any(_normalise_for_match(a) in haystack for a in acronyms):
                continue
            flags.append(
                f"FLAG [CERTIFICATIONS]: '{Agent.truncate(head, 90)}' is not listed on your "
                "LinkedIn profile or uploaded resume. A fabricated credential is the single most "
                "damaging thing on a resume — verify it or delete the line."
            )
            if len(flags) >= _MAX_FLAGS:
                return flags

        # --- years ------------------------------------------------------------
        doc_years = {
            year
            for name in header_sections
            for year in _YEAR_RE.findall(sections.get(name) or "")
        }
        unknown_years = sorted(y for y in doc_years if y not in allowed_years)
        if unknown_years:
            flags.append(
                "FLAG [dates]: year(s) " + ", ".join(unknown_years)
                + " appear in the resume but not in the source profile or uploaded resume. "
                "Check every employment and education date."
            )

        # --- metrics ----------------------------------------------------------
        unverified: list[str] = []
        for bullet in collect_bullets(markdown, sections=sections):
            for match in _METRIC_RE.finditer(bullet):
                metric = match.group(0).strip()
                norm = _normalise_for_match(metric)
                digits = re.sub(r"\D", "", metric)
                if not digits or len(digits) <= 1:
                    continue  # "3 months", "2x" style noise is not a claim worth flagging
                if norm and norm in haystack:
                    continue
                if digits in re.sub(r"\D", "", haystack):
                    continue
                unverified.append(f"'{metric}' in: {Agent.truncate(bullet, 110)}")
                break
        if unverified:
            flags.append(
                f"FLAG [metrics]: {len(unverified)} bullet(s) contain a number that is not in the "
                "source material. Confirm each figure or rewrite the bullet without it — "
                + " ; ".join(unverified[:4])
            )

        # --- target employer used as a past employer --------------------------
        company = Agent.clean_str((job or {}).get("company"), 160)
        if company:
            norm_company = _normalise_for_match(company)
            experience = _normalise_for_match(sections.get("PROFESSIONAL EXPERIENCE") or "")
            if norm_company and norm_company in experience and norm_company not in haystack:
                flags.append(
                    f"FLAG [target employer]: '{company}' is the company being applied to but "
                    "appears inside PROFESSIONAL EXPERIENCE. Remove it."
                )
        return flags[:_MAX_FLAGS]

    @staticmethod
    def _checks(
        markdown: str, sections: dict[str, str], contact: dict[str, str], problems: list[str]
    ) -> dict[str, Any]:
        bullets = collect_bullets(markdown, sections=sections)
        verb_ok = sum(
            1 for bullet in bullets if stem(_first_word(bullet)) in _ACTION_VERB_STEMS
        )
        return {
            "word_count": len(re.findall(r"\b[\w'\-]+\b", markdown or "")),
            "bullet_count": len(bullets),
            "action_verb_bullets": verb_ok,
            "sections_present": [name for name in CANONICAL_SECTIONS if name in sections],
            "sections_missing_required": [name for name in REQUIRED_SECTIONS if name not in sections],
            "sections_missing_optional": [name for name in OPTIONAL_SECTIONS if name not in sections],
            "contact_present": {key: bool(value) for key, value in contact.items()},
            "unresolved_problems": list(problems),
        }

    @staticmethod
    def _rationale(
        job: dict[str, Any] | None,
        jd_keywords: list[str],
        used: list[str],
        missing: list[str],
        notes: Iterable[str],
        flags: list[str],
        checks: dict[str, Any],
        contact: dict[str, str],
        problems: list[str],
    ) -> list[str]:
        """Human-readable account of what this build did and what needs attention."""
        out: list[str] = []
        if job:
            out.append(
                f"Tailored for '{Agent.clean_str(job.get('title'), 160) or 'the target role'}' at "
                f"'{Agent.clean_str(job.get('company'), 160) or 'the target company'}'"
                + (
                    f"; mirrored {len(used)}/{len(jd_keywords)} of the highest-weighted job-description terms."
                    if jd_keywords
                    else "; no job description was available, so tailoring used the title and company only."
                )
            )
        else:
            out.append(
                f"General-purpose base resume; covered {len(used)} terms from the profile keyword bank."
            )
        if used:
            out.append("Keywords worked in: " + ", ".join(used[:20]))
        if missing:
            out.append(
                "Target terms still absent (add them only where true): " + ", ".join(missing[:15])
            )
        for note in list(notes)[:6]:
            out.append("Applied tailoring note: " + Agent.clean_str(note, 300))
        out.append(
            f"Structure: {', '.join(checks['sections_present']) or 'none'} — "
            f"{checks['word_count']} words, {checks['bullet_count']} bullets, "
            f"{checks['action_verb_bullets']} opening with a strong action verb."
        )
        if checks["sections_missing_required"]:
            out.append(
                "Missing required section(s): " + ", ".join(checks["sections_missing_required"])
                + " — the ATS parseability score is capped until they exist."
            )
        absent_contact = [
            key for key in ("email", "phone", "location", "linkedin") if not contact.get(key)
        ]
        if absent_contact:
            out.append(
                "No " + ", ".join(absent_contact) + " was available in the LinkedIn profile or the "
                "uploaded resume, so it was left out rather than invented. Add it manually — "
                "recruiters and parsers both filter on the contact block."
            )
        if problems:
            out.append("Unresolved formatting issues after revision: " + "; ".join(problems))
        if flags:
            out.append(
                f"{len(flags)} factual claim(s) could not be traced back to your source data and are "
                "flagged below. Hermes did not remove them — verify each one before sending this resume."
            )
            out.extend(flags)
        else:
            out.append(
                "Anti-fabrication audit passed: every organisation, date and metric in the document "
                "was traced back to your LinkedIn profile, analysis or uploaded resume."
            )
        return out
