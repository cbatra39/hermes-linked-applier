"""JobScout -- find LinkedIn job postings through the LinkedIn MCP server.

This agent is deliberately **LLM-free**. Its job is to call one browser-driven
MCP tool and turn whatever comes back into rows ``hermes.pipeline._upsert_job``
can persist. Inventing or "improving" job data with a language model would be
actively harmful here: a hallucinated job id produces an apply link that 404s.

Why the normalisation is so defensive
-------------------------------------
``search_jobs`` scrapes a real LinkedIn results page with a headless browser, so
its payload shape is a function of LinkedIn's DOM on the day. Observed shapes
include a bare JSON array, ``{"jobs": [...]}``, ``{"results": {...}}``,
``{"items": [...]}`` (which is also what ``LinkedInMCP`` produces when a tool
returns a top-level array), a single job object, jobs nested two or three levels
inside a wrapper, and -- when the scraper cannot parse the page at all -- a
``{"text": "..."}`` blob. All of those are handled, and job ids are recovered
from ``urn:li:fsd_jobPosting:4123456789`` strings and from URLs
(``/jobs/view/4123456789/``, ``?currentJobId=4123456789``) when no id field
exists.

Verified tool parameters (tested live against
``stickerdaniel/linkedin-mcp-server:4.23.2``): ``keywords`` (required),
``location``, ``max_pages``, ``date_posted``, ``job_type``,
``experience_level``, ``work_type``, ``easy_apply``, ``sort_by``.

**There is no apply tool.** This agent produces an apply URL for a human to
click; nothing here submits an application, and nothing here ever will.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from hermes.agents.base import Agent, AgentError
from hermes.mcp_client import MCPAuthError, MCPError

log = logging.getLogger(__name__)

__all__ = ["JobScout", "LINKEDIN_JOB_URL"]

#: Canonical, tracking-free apply URL for a LinkedIn job id.
LINKEDIN_JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

_LINKEDIN_HOST_RE = re.compile(r"(?:^|\.)linkedin\.com$", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"\b(\d{6,14})\b")
_URN_RE = re.compile(r"urn:li:[a-zA-Z_]*[jJ]ob[a-zA-Z_]*:(\d{6,14})")
_VIEW_URL_RE = re.compile(r"/jobs/(?:view|posting)/(?:[^/?#]*-)?(\d{6,14})", re.IGNORECASE)
_CURRENT_JOB_RE = re.compile(r"[?&]currentJobId=(\d{6,14})", re.IGNORECASE)

#: Keys that may hold a job identifier, in priority order.
_ID_KEYS: tuple[str, ...] = (
    "linkedin_job_id", "job_id", "jobId", "jobPostingId", "job_posting_id",
    "posting_id", "id", "job_urn", "jobUrn", "entity_urn", "entityUrn", "urn",
    "tracking_urn", "reference_id",
)
_TITLE_KEYS: tuple[str, ...] = ("title", "job_title", "jobTitle", "position", "role", "name", "headline")
_COMPANY_KEYS: tuple[str, ...] = (
    "company", "company_name", "companyName", "organisation", "organization",
    "employer", "hiring_company", "company_universal_name",
)
_LOCATION_KEYS: tuple[str, ...] = (
    "location", "job_location", "jobLocation", "formatted_location", "place",
    "city", "region", "workplace", "geo",
)
_URL_KEYS: tuple[str, ...] = (
    "url", "job_url", "jobUrl", "link", "permalink", "apply_url", "applyUrl",
    "job_posting_url", "jobPostingUrl", "href", "canonical_url",
)
_POSTED_KEYS: tuple[str, ...] = (
    "posted", "posted_at", "postedAt", "posted_date", "date_posted", "datePosted",
    "listed_at", "listedAt", "posted_time_ago", "time_posted", "publish_date",
    "created_at", "posted_on", "age",
)
_DESCRIPTION_KEYS: tuple[str, ...] = (
    "description", "job_description", "jobDescription", "details", "job_details",
    "text", "content", "body", "summary", "full_description",
)
_EASY_APPLY_KEYS: tuple[str, ...] = (
    "easy_apply", "easyApply", "is_easy_apply", "isEasyApply", "apply_method",
    "applyMethod", "application_type",
)
_SENIORITY_KEYS: tuple[str, ...] = ("experience_level", "seniority_level", "seniority", "level")
_JOB_TYPE_KEYS: tuple[str, ...] = ("job_type", "employment_type", "employmentType", "type")
_WORK_TYPE_KEYS: tuple[str, ...] = ("work_type", "workplace_type", "remote", "work_mode")
_APPLICANTS_KEYS: tuple[str, ...] = ("applicants", "applicant_count", "num_applicants", "applies")

#: Wrapper keys that commonly hold the job collection.
_LIST_HINT_KEYS: frozenset[str] = frozenset(
    {
        "jobs", "results", "items", "data", "postings", "jobpostings", "job_postings",
        "elements", "content", "hits", "records", "list", "values", "matches", "entries",
    }
)

#: Client-side spelling normalisation for the enum-ish MCP filters. Only reshapes
#: obvious user spellings toward the documented value; unknown values are passed
#: through untouched so the server (the authority) can accept or reject them.
_DATE_POSTED_ALIASES: dict[str, str] = {
    "24h": "past-24h", "past24h": "past-24h", "past-24-hours": "past-24h",
    "past24hours": "past-24h", "day": "past-24h", "today": "past-24h",
    "1day": "past-24h", "last-24-hours": "past-24h", "past-day": "past-24h",
    "week": "past-week", "pastweek": "past-week", "last-week": "past-week",
    "7days": "past-week", "past-7-days": "past-week",
    "month": "past-month", "pastmonth": "past-month", "last-month": "past-month",
    "30days": "past-month", "past-30-days": "past-month",
    "any": "any-time", "anytime": "any-time", "all": "any-time",
}
_WORK_TYPE_ALIASES: dict[str, str] = {
    "onsite": "on-site", "on-site": "on-site", "office": "on-site",
    "remote": "remote", "wfh": "remote", "work-from-home": "remote",
    "hybrid": "hybrid",
}
_JOB_TYPE_ALIASES: dict[str, str] = {
    "fulltime": "full-time", "full-time": "full-time", "permanent": "full-time",
    "parttime": "part-time", "part-time": "part-time",
    "contract": "contract", "contractor": "contract", "c2h": "contract",
    "temporary": "temporary", "temp": "temporary",
    "internship": "internship", "intern": "internship",
    "volunteer": "volunteer",
}
_EXPERIENCE_ALIASES: dict[str, str] = {
    "intern": "internship", "internship": "internship",
    "entry": "entry-level", "entrylevel": "entry-level", "entry-level": "entry-level",
    "junior": "entry-level", "graduate": "entry-level",
    "associate": "associate",
    "mid": "mid-senior-level", "midsenior": "mid-senior-level",
    "mid-senior": "mid-senior-level", "mid-senior-level": "mid-senior-level",
    "senior": "mid-senior-level",
    "director": "director", "executive": "executive", "vp": "executive",
}
_SORT_BY_ALIASES: dict[str, str] = {
    "relevance": "relevance", "relevant": "relevance", "best": "relevance",
    "date": "date", "recent": "date", "newest": "date", "most-recent": "date",
}


def _slug(value: Any) -> str:
    """Lowercase, hyphen-joined form used for alias lookups."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(value or "").lower())).strip("-")


def _alias(value: Any, table: dict[str, str]) -> str | None:
    """Map a user-supplied filter value onto its documented spelling."""
    raw = Agent.clean_str(value, 60)
    if not raw:
        return None
    slug = _slug(raw)
    if not slug:
        return None
    return table.get(slug) or table.get(slug.replace("-", "")) or raw


def _digits_from(value: Any) -> str:
    """Extract a LinkedIn numeric job id from an id field, a URN or a URL."""
    text = Agent.clean_str(value, 400)
    if not text:
        return ""
    if text.isdigit() and 6 <= len(text) <= 14:
        return text
    for pattern in (_URN_RE, _VIEW_URL_RE, _CURRENT_JOB_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    # A bare id embedded in some other string ("job-4123456789").
    if not text.startswith("http"):
        match = _JOB_ID_RE.search(text)
        if match:
            return match.group(1)
    return ""


def _looks_like_job(node: dict[str, Any]) -> bool:
    """Heuristic: does this dict describe a single job posting?"""
    if not node:
        return False
    has_identity = any(
        Agent.first_of(node, key) not in (None, "", [], {}) for key in _ID_KEYS + _URL_KEYS
    )
    has_content = any(
        Agent.first_of(node, key) not in (None, "", [], {})
        for key in _TITLE_KEYS + _COMPANY_KEYS + _DESCRIPTION_KEYS
    )
    return bool(has_identity and has_content) or bool(
        has_identity and Agent.first_of(node, "posted", "posted_at", "listed_at")
    )


def _collect_job_dicts(node: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Walk an arbitrary payload and collect every dict that looks like a job."""
    if depth > 7 or len(out) > 2000:
        return
    if isinstance(node, dict):
        if _looks_like_job(node):
            out.append(node)
            return
        # Prefer the conventional collection keys, then fall back to everything.
        preferred = [v for k, v in node.items() if re.sub(r"[^a-z]", "", str(k).lower()) in _LIST_HINT_KEYS]
        for value in preferred:
            _collect_job_dicts(value, out, depth + 1)
        if out:
            return
        for value in node.values():
            _collect_job_dicts(value, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_job_dicts(value, out, depth + 1)


def _jobs_from_text(text: str) -> list[dict[str, Any]]:
    """Last resort: recover job ids from an unparsed text blob.

    Better a bare apply link the user can click (and that enrichment can fill in)
    than throwing away a scrape the server did perform.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for pattern in (_VIEW_URL_RE, _CURRENT_JOB_RE, _URN_RE):
        for match in pattern.finditer(text or ""):
            job_id = match.group(1)
            if job_id in seen:
                continue
            seen.add(job_id)
            out.append({"job_id": job_id, "url": LINKEDIN_JOB_URL.format(job_id=job_id)})
    return out


class JobScout(Agent):
    """Search LinkedIn jobs over MCP and normalise the results for persistence."""

    NAME = "job-scout"

    #: Documented for the Settings page; this agent makes no LLM calls.
    SYSTEM = (
        "JobScout performs no LLM inference. It calls the LinkedIn MCP `search_jobs` tool, "
        "normalises the scraped payload and derives a canonical apply URL. Ranking is "
        "MatchRanker's job; applying is the human's."
    )

    #: Enrichment ceiling. Each get_job_details call drives a real browser page load
    #: (seconds to a minute) and counts against LinkedIn's tolerance for scraping.
    MAX_DETAIL_CALLS = 60

    #: Stop enriching after this many consecutive failures -- something systemic is
    #: wrong (session died, layout changed) and 25 more timeouts will not help.
    DETAIL_FAILURE_LIMIT = 3

    async def search(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        *,
        fetch_details: bool = True,
        limit_details: int = 25,
        **extra: Any,
    ) -> list[dict[str, Any]]:
        """Search LinkedIn and return normalised job rows.

        Parameters mirror the verified MCP ``search_jobs`` signature.
        ``fetch_details`` / ``limit_details`` are Hermes-side enrichment controls,
        not MCP arguments: the top ``limit_details`` results that came back
        without a description are re-fetched through ``get_job_details``.

        Returns
        -------
        list of ``{"linkedin_job_id","title","company","location","url",
        "posted","easy_apply","description","raw"}`` -- exactly what
        ``pipeline._upsert_job`` consumes. ``url`` is always a clickable apply
        link; Hermes never submits the application.
        """
        mcp = self._require_mcp()
        query = Agent.clean_str(keywords, 500)
        if not query:
            raise AgentError(
                "JobScout.search needs non-empty `keywords` -- LinkedIn's job search "
                "rejects an empty query. Set it on the Jobs page or via the "
                "`default_job_keywords` setting."
            )
        if extra:
            await self.emit(
                "Ignoring unsupported search parameter(s): " + ", ".join(sorted(extra)) + ". "
                "The verified MCP search_jobs arguments are keywords, location, max_pages, "
                "date_posted, job_type, experience_level, work_type, easy_apply, sort_by.",
                "warn",
            )

        pages = self._pages(max_pages)
        args: dict[str, Any] = {
            "keywords": query,
            "location": Agent.clean_str(location, 200) or None,
            "max_pages": pages,
            "date_posted": _alias(date_posted, _DATE_POSTED_ALIASES),
            "job_type": _alias(job_type, _JOB_TYPE_ALIASES),
            "experience_level": _alias(experience_level, _EXPERIENCE_ALIASES),
            "work_type": _alias(work_type, _WORK_TYPE_ALIASES),
            "easy_apply": bool(easy_apply),
            "sort_by": _alias(sort_by, _SORT_BY_ALIASES),
        }
        applied = {k: v for k, v in args.items() if v not in (None, "", False) and k != "keywords"}
        await self.emit(
            f"Searching LinkedIn for '{query}'"
            + (f" ({', '.join(f'{k}={v}' for k, v in applied.items())})" if applied else "")
            + f" — up to {pages} page(s). This drives a real browser and takes ~{pages * 45}-{pages * 90}s."
        )

        payload = await mcp.search_jobs(
            keywords=query,
            location=args["location"],
            max_pages=pages,
            date_posted=args["date_posted"],
            job_type=args["job_type"],
            experience_level=args["experience_level"],
            work_type=args["work_type"],
            easy_apply=bool(easy_apply),
            sort_by=args["sort_by"],
            run_id=self.run_id,
        )

        jobs = self._normalise_payload(payload)
        if not jobs:
            await self.emit(
                f"LinkedIn returned no usable postings for '{query}'"
                + (f" in {args['location']}" if args["location"] else "")
                + ". Broaden the keywords, drop the filters, or raise max_pages.",
                "warn",
            )
            return []

        await self.emit(
            f"{len(jobs)} posting(s) parsed"
            f" — {sum(1 for j in jobs if j['description'])} arrived with a description."
        )

        if fetch_details:
            await self._enrich(jobs, limit_details)

        missing_desc = [j for j in jobs if not j["description"]]
        if missing_desc:
            await self.emit(
                f"{len(missing_desc)} posting(s) still have no description; their match scores and "
                "tailored resumes will be weaker. Raise limit_details to enrich more.",
                "warn" if len(missing_desc) > len(jobs) // 2 else "info",
            )
        return jobs

    # -------------------------------------------------------------- normalising

    @staticmethod
    def _pages(max_pages: Any) -> int:
        """The MCP tool accepts 1..10 pages (verified); clamp rather than fail."""
        try:
            pages = int(max_pages)
        except (TypeError, ValueError):
            pages = 3
        return max(1, min(10, pages))

    def _normalise_payload(self, payload: Any) -> list[dict[str, Any]]:
        """Turn any ``search_jobs`` payload shape into de-duplicated job rows."""
        raw_jobs: list[dict[str, Any]] = []
        _collect_job_dicts(payload, raw_jobs)

        if not raw_jobs and isinstance(payload, dict):
            # The scraper could not build JSON: mine ids out of the text blob.
            text = Agent.clean_str(
                Agent.first_of(payload, "text", "content", "message", "output", default=""), None
            )
            if text:
                raw_jobs = _jobs_from_text(text)
                if raw_jobs:
                    log.info("recovered %d job id(s) from an unstructured MCP payload", len(raw_jobs))
                else:
                    log.warning("MCP search_jobs returned unstructured text: %.300s", text)

        rows: dict[str, dict[str, Any]] = {}
        skipped = 0
        for node in raw_jobs:
            row = self._job_row(node)
            if row is None:
                skipped += 1
                continue
            key = row["linkedin_job_id"]
            existing = rows.get(key)
            if existing is None:
                rows[key] = row
                continue
            # Same posting seen twice across pages: keep the richer copy.
            for field in ("title", "company", "location", "posted", "description"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]
            if row.get("easy_apply"):
                existing["easy_apply"] = True
        if skipped:
            log.info("JobScout skipped %d payload node(s) with no recoverable job id", skipped)
        return list(rows.values())

    def _job_row(self, node: dict[str, Any]) -> dict[str, Any] | None:
        """One scraped node -> one persistable row (or None if it has no id)."""
        if not isinstance(node, dict):
            return None

        job_id = ""
        for key in _ID_KEYS:
            job_id = _digits_from(Agent.first_of(node, key))
            if job_id:
                break
        scraped_url = Agent.clean_str(Agent.first_of(node, *_URL_KEYS, default=""), 800)
        if not job_id:
            job_id = _digits_from(scraped_url)
        if not job_id:
            # Some payloads bury the urn in a nested tracking object.
            job_id = _digits_from(Agent.compact_json(node, 4000))
        if not job_id:
            return None

        title = Agent.clean_str(Agent.first_of(node, *_TITLE_KEYS, default=""), 400)
        company = Agent.clean_str(Agent.first_of(node, *_COMPANY_KEYS, default=""), 300)
        location = Agent.clean_str(Agent.first_of(node, *_LOCATION_KEYS, default=""), 300)
        posted = Agent.clean_str(Agent.first_of(node, *_POSTED_KEYS, default=""), 120)
        description = Agent.clean_str(Agent.first_of(node, *_DESCRIPTION_KEYS, default=""), None)

        return {
            "linkedin_job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "url": self._apply_url(scraped_url, job_id),
            "posted": posted,
            "easy_apply": self._easy_apply(node),
            "description": description,
            "raw": {
                **{k: v for k, v in node.items() if k not in ("raw",)},
                "_hermes": {
                    "scraped_url": scraped_url,
                    "experience_level": Agent.clean_str(
                        Agent.first_of(node, *_SENIORITY_KEYS, default=""), 80
                    ),
                    "job_type": Agent.clean_str(Agent.first_of(node, *_JOB_TYPE_KEYS, default=""), 80),
                    "work_type": Agent.clean_str(Agent.first_of(node, *_WORK_TYPE_KEYS, default=""), 80),
                    "applicants": Agent.clean_str(
                        Agent.first_of(node, *_APPLICANTS_KEYS, default=""), 40
                    ),
                },
            },
        }

    @staticmethod
    def _easy_apply(node: dict[str, Any]) -> bool:
        value = Agent.first_of(node, *_EASY_APPLY_KEYS, default=None)
        if isinstance(value, bool):
            return value
        text = Agent.clean_str(value, 80).lower()
        if not text:
            return False
        if text in ("true", "yes", "1", "y"):
            return True
        return "easy" in text

    @staticmethod
    def _apply_url(scraped: str, job_id: str) -> str:
        """A clean, clickable apply URL.

        LinkedIn's scraped hrefs are frequently relative and always carry tracking
        parameters, so a LinkedIn job with a known id is always rewritten to the
        canonical ``/jobs/view/<id>/`` form. A genuinely external URL is kept.
        """
        url = (scraped or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://www.linkedin.com" + url
        elif url and not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url if "." in url.split("/")[0] else ""

        if url:
            try:
                host = urlsplit(url).netloc.split("@")[-1].split(":")[0]
            except ValueError:
                host = ""
            if host and not _LINKEDIN_HOST_RE.search(host):
                return url  # an external ATS link is more useful than a rewrite
        if job_id:
            return LINKEDIN_JOB_URL.format(job_id=job_id)
        return url

    # --------------------------------------------------------------- enrichment

    async def _enrich(self, jobs: list[dict[str, Any]], limit_details: Any) -> None:
        """Fill in missing descriptions for the top N results, in place."""
        try:
            limit = int(limit_details)
        except (TypeError, ValueError):
            limit = 25
        limit = max(0, min(self.MAX_DETAIL_CALLS, limit))
        if not limit:
            return

        targets = [job for job in jobs[: max(limit * 2, limit)] if not job["description"]][:limit]
        if not targets:
            await self.emit("Every posting already carries a description; skipping enrichment.")
            return

        await self.emit(
            f"Fetching full details for the top {len(targets)} posting(s) via get_job_details. "
            "Each one is a browser page load, so this is the slow part."
        )
        mcp = self._require_mcp()
        ok = 0
        consecutive_failures = 0

        for index, job in enumerate(targets, 1):
            job_id = job["linkedin_job_id"]
            try:
                payload = await mcp.get_job_details(job_id, run_id=self.run_id)
            except MCPAuthError as exc:
                await self.emit(
                    "LinkedIn session became invalid during enrichment; keeping the postings "
                    f"already found and stopping detail fetches. {exc}",
                    "error",
                )
                return
            except MCPError as exc:
                consecutive_failures += 1
                await self.emit(f"Could not fetch details for job {job_id}: {exc}", "warn")
                if consecutive_failures >= self.DETAIL_FAILURE_LIMIT:
                    await self.emit(
                        f"{consecutive_failures} consecutive detail fetches failed — stopping "
                        "enrichment. The postings themselves are unaffected.",
                        "warn",
                    )
                    return
                continue
            except Exception as exc:  # pragma: no cover - defensive
                consecutive_failures += 1
                log.warning("unexpected get_job_details failure for %s: %s", job_id, exc)
                await self.emit(f"Unexpected error fetching job {job_id}: {type(exc).__name__}: {exc}", "warn")
                if consecutive_failures >= self.DETAIL_FAILURE_LIMIT:
                    return
                continue

            consecutive_failures = 0
            if self._merge_details(job, payload):
                ok += 1
            if index % 5 == 0 or index == len(targets):
                await self.emit(f"Enriched {index}/{len(targets)} posting(s).")

        await self.emit(f"Detail fetch complete — {ok}/{len(targets)} posting(s) gained a description.")

    def _merge_details(self, job: dict[str, Any], payload: Any) -> bool:
        """Merge a ``get_job_details`` payload into a job row. True if it helped."""
        nodes: list[dict[str, Any]] = []
        _collect_job_dicts(payload, nodes)
        node: dict[str, Any] = nodes[0] if nodes else (payload if isinstance(payload, dict) else {})
        if not node:
            return False

        description = Agent.clean_str(Agent.first_of(node, *_DESCRIPTION_KEYS, default=""), None)
        if not description and isinstance(payload, dict):
            description = Agent.clean_str(Agent.first_of(payload, "text", default=""), None)

        changed = False
        if description and len(description) > len(job.get("description") or ""):
            job["description"] = description
            changed = True
        for field, keys in (
            ("title", _TITLE_KEYS),
            ("company", _COMPANY_KEYS),
            ("location", _LOCATION_KEYS),
            ("posted", _POSTED_KEYS),
        ):
            if not job.get(field):
                value = Agent.clean_str(Agent.first_of(node, *keys, default=""), 400)
                if value:
                    job[field] = value
                    changed = True
        if not job.get("easy_apply") and self._easy_apply(node):
            job["easy_apply"] = True
        if not job.get("url"):
            job["url"] = self._apply_url(
                Agent.clean_str(Agent.first_of(node, *_URL_KEYS, default=""), 800),
                job["linkedin_job_id"],
            )

        raw = job.setdefault("raw", {})
        if isinstance(raw, dict):
            raw["details"] = node
        return changed

    # ----------------------------------------------------------- saved postings

    async def saved_jobs(self, max_pages: int = 3) -> list[dict[str, Any]]:
        """Normalised rows for the account's saved jobs (``get_saved_jobs`` tool).

        Same output shape as :meth:`search`, so the pipeline can persist these
        rows through exactly the same path.
        """
        mcp = self._require_mcp()
        pages = self._pages(max_pages)
        await self.emit(f"Reading your saved LinkedIn jobs (up to {pages} page(s))…")
        payload = await mcp.get_saved_jobs(pages, run_id=self.run_id)
        rows = self._normalise_payload(payload)
        await self.emit(f"{len(rows)} saved posting(s) parsed.")
        return rows

    # ---------------------------------------------------------------- utilities

    @staticmethod
    def apply_url_for(job_id: Any) -> str:
        """Canonical apply URL for a LinkedIn job id (used by the API layer)."""
        digits = _digits_from(job_id)
        return LINKEDIN_JOB_URL.format(job_id=digits) if digits else ""

    @staticmethod
    def summarise(jobs: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Cheap counts for a run's result payload."""
        items = list(jobs)
        return {
            "count": len(items),
            "with_description": sum(1 for job in items if job.get("description")),
            "easy_apply": sum(1 for job in items if job.get("easy_apply")),
            "companies": len({(job.get("company") or "").lower() for job in items if job.get("company")}),
        }
