"""Deterministic (and optionally LLM-augmented) ATS resume scoring.

HONESTY NOTICE
--------------
``score_resume_deterministic`` is a **heuristic proxy**, not a real Applicant
Tracking System.  Workday, Greenhouse, Taleo, iCIMS, SuccessFactors and Lever
each use different parsers, different keyword models and different (usually
undisclosed, frequently recruiter-configured) ranking rules.  Nobody outside
those vendors can compute "your Workday score", and any tool claiming a
specific vendor number is guessing.

What this module *does* do is measure the properties that every mainstream
parser and every recruiter search demonstrably cares about:

* the document parses into recognisable sections at all,
* the vocabulary of the target job description actually appears in the text,
* contact details are machine-extractable,
* bullets read as quantified accomplishments rather than duty lists,
* formatting is boring and consistent,
* prose is scannable by a human in the six seconds they will give it.

Treat the output as actionable feedback ("you are missing these 9 keywords",
"31% of your bullets have no metric"), not as a prophecy about one employer's
pipeline.

The scorer is pure Python -- no LLM, no network, no I/O -- so it runs in
milliseconds and works offline.  ``score_resume`` optionally layers an LLM
semantic-fit pass on top and blends the two numbers.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "ACTION_VERBS",
    "CANONICAL_SECTIONS",
    "REQUIRED_SECTIONS",
    "OPTIONAL_SECTIONS",
    "STOPWORDS",
    "WEIGHTS",
    "score_resume_deterministic",
    "score_resume",
    "split_sections",
    "collect_bullets",
    "extract_keywords",
    "tokenize",
    "stem",
    "normalize_token",
]


# ---------------------------------------------------------------------------
# Canonical structure (must stay in sync with ResumeArchitect's output)
# ---------------------------------------------------------------------------

#: Canonical section headings, in the exact order the ResumeArchitect emits them.
CANONICAL_SECTIONS: list[str] = [
    "PROFESSIONAL SUMMARY",
    "CORE COMPETENCIES",
    "PROFESSIONAL EXPERIENCE",
    "PROJECTS",
    "EDUCATION",
    "CERTIFICATIONS",
    "TECHNICAL SKILLS",
]

#: Sections whose absence is a genuine parsing/ATS problem.
REQUIRED_SECTIONS: list[str] = [
    "PROFESSIONAL SUMMARY",
    "CORE COMPETENCIES",
    "PROFESSIONAL EXPERIENCE",
    "EDUCATION",
    "TECHNICAL SKILLS",
]

#: Sections that are only appropriate when the candidate actually has the data.
#: Omitting them is not penalised (an empty "PROJECTS" heading looks worse to a
#: human reviewer than no heading at all).
OPTIONAL_SECTIONS: list[str] = ["PROJECTS", "CERTIFICATIONS"]

#: Common real-world heading synonyms mapped onto the canonical names, so we
#: can score resumes the user uploaded rather than ones Hermes generated.
_SECTION_ALIASES: dict[str, str] = {
    "SUMMARY": "PROFESSIONAL SUMMARY",
    "PROFILE": "PROFESSIONAL SUMMARY",
    "EXECUTIVE SUMMARY": "PROFESSIONAL SUMMARY",
    "PROFESSIONAL PROFILE": "PROFESSIONAL SUMMARY",
    "CAREER SUMMARY": "PROFESSIONAL SUMMARY",
    "OBJECTIVE": "PROFESSIONAL SUMMARY",
    "CAREER OBJECTIVE": "PROFESSIONAL SUMMARY",
    "ABOUT": "PROFESSIONAL SUMMARY",
    "ABOUT ME": "PROFESSIONAL SUMMARY",
    "CORE COMPETENCES": "CORE COMPETENCIES",
    "KEY COMPETENCIES": "CORE COMPETENCIES",
    "AREAS OF EXPERTISE": "CORE COMPETENCIES",
    "KEY SKILLS": "CORE COMPETENCIES",
    "HIGHLIGHTS": "CORE COMPETENCIES",
    "KEY ACHIEVEMENTS": "CORE COMPETENCIES",
    "EXPERIENCE": "PROFESSIONAL EXPERIENCE",
    "WORK EXPERIENCE": "PROFESSIONAL EXPERIENCE",
    "EMPLOYMENT": "PROFESSIONAL EXPERIENCE",
    "EMPLOYMENT HISTORY": "PROFESSIONAL EXPERIENCE",
    "WORK HISTORY": "PROFESSIONAL EXPERIENCE",
    "RELEVANT EXPERIENCE": "PROFESSIONAL EXPERIENCE",
    "PROFESSIONAL BACKGROUND": "PROFESSIONAL EXPERIENCE",
    "PROJECT EXPERIENCE": "PROJECTS",
    "SELECTED PROJECTS": "PROJECTS",
    "KEY PROJECTS": "PROJECTS",
    "ACADEMIC BACKGROUND": "EDUCATION",
    "EDUCATION AND TRAINING": "EDUCATION",
    "ACADEMIC QUALIFICATIONS": "EDUCATION",
    "CERTIFICATION": "CERTIFICATIONS",
    "LICENSES AND CERTIFICATIONS": "CERTIFICATIONS",
    "CERTIFICATIONS AND LICENSES": "CERTIFICATIONS",
    "CERTIFICATES": "CERTIFICATIONS",
    "SKILLS": "TECHNICAL SKILLS",
    "TECHNICAL EXPERTISE": "TECHNICAL SKILLS",
    "TECHNOLOGIES": "TECHNICAL SKILLS",
    "TOOLS AND TECHNOLOGIES": "TECHNICAL SKILLS",
    "TECHNICAL PROFICIENCIES": "TECHNICAL SKILLS",
}


# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------

#: Strong resume action verbs, stored in base form.  Matching is done on stems
#: so "Led"/"Leading", "Delivered"/"Delivering" all resolve correctly.
ACTION_VERBS: frozenset[str] = frozenset(
    {
        # leadership / ownership
        "led", "directed", "managed", "supervised", "headed", "chaired", "owned",
        "spearheaded", "orchestrated", "governed", "oversaw", "mentored", "coached",
        "trained", "onboarded", "delegated", "championed", "drove", "steered",
        # building / delivering
        "built", "developed", "engineered", "designed", "architected", "implemented",
        "deployed", "delivered", "launched", "shipped", "created", "produced",
        "authored", "coded", "programmed", "prototyped", "configured", "installed",
        "integrated", "migrated", "provisioned", "instrumented", "containerized",
        # improving
        "improved", "optimized", "streamlined", "accelerated", "automated",
        "reduced", "cut", "eliminated", "increased", "boosted", "grew", "scaled",
        "enhanced", "refactored", "modernized", "standardized", "consolidated",
        "simplified", "hardened", "stabilized", "tuned", "upgraded", "rearchitected",
        # analysis / discovery
        "analyzed", "analysed", "assessed", "audited", "evaluated", "investigated",
        "diagnosed", "researched", "modeled", "modelled", "forecasted", "quantified",
        "benchmarked", "measured", "monitored", "validated", "verified", "tested",
        "reviewed", "identified", "uncovered", "traced", "profiled", "mapped",
        # coordination / influence
        "coordinated", "collaborated", "partnered", "facilitated", "negotiated",
        "presented", "advised", "consulted", "influenced", "aligned", "liaised",
        "communicated", "briefed", "documented", "reported", "recommended",
        # process / delivery management
        "planned", "prioritized", "executed", "administered", "maintained",
        "operated", "resolved", "remediated", "mitigated", "troubleshot",
        "supported", "sustained", "enforced", "ensured", "established",
        "instituted", "formalized", "rolled", "piloted", "transitioned",
        # commercial
        "generated", "captured", "won", "secured", "saved", "recovered",
        "sourced", "procured", "forecast", "budgeted", "invoiced", "monetized",
    }
)

#: English stopwords plus resume/JD boilerplate that carries no signal.
STOPWORDS: frozenset[str] = frozenset(
    {
        # articles / pronouns / conjunctions / prepositions
        "a", "an", "the", "and", "or", "but", "nor", "so", "yet", "for", "of",
        "to", "in", "on", "at", "by", "with", "from", "into", "onto", "upon",
        "about", "above", "across", "after", "against", "along", "among",
        "around", "as", "before", "behind", "below", "beneath", "beside",
        "between", "beyond", "during", "except", "inside", "near", "off",
        "outside", "over", "past", "since", "through", "throughout", "toward",
        "towards", "under", "until", "up", "via", "within", "without",
        "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your",
        "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "they",
        "them", "their", "theirs", "this", "that", "these", "those", "who",
        "whom", "whose", "which", "what", "where", "when", "why", "how",
        # verbs with no discriminating power
        "is", "am", "are", "was", "were", "be", "been", "being", "do", "does",
        "did", "doing", "done", "have", "has", "had", "having", "will", "would",
        "shall", "should", "can", "could", "may", "might", "must", "ought",
        "get", "gets", "got", "make", "makes", "made", "take", "takes", "go",
        "goes", "going", "use", "uses", "using", "used", "help", "helps",
        "need", "needs", "want", "wants", "know", "knows", "like", "likes",
        "include", "includes", "including", "included",
        # generic adverbs / adjectives
        "very", "more", "most", "much", "many", "some", "any", "all", "both",
        "each", "few", "less", "least", "other", "others", "another", "same",
        "such", "no", "not", "only", "own", "than", "then", "there", "here",
        "also", "well", "just", "even", "still", "already", "always", "never",
        "often", "sometimes", "usually", "new", "good", "great", "strong",
        "excellent", "high", "highly", "large", "small", "best", "better",
        "able", "ability", "across", "etc", "e", "g", "ie", "eg",
        # job-posting boilerplate
        "job", "jobs", "role", "roles", "position", "positions", "title",
        "company", "companies", "organization", "organisation", "employer",
        "candidate", "candidates", "applicant", "applicants", "application",
        "apply", "applying", "resume", "cv", "cover", "letter", "hiring",
        "recruiter", "recruiting", "recruitment", "interview", "team", "teams",
        "work", "working", "works", "worked", "workplace", "environment",
        "opportunity", "opportunities", "responsibility", "responsibilities",
        "requirement", "requirements", "qualification", "qualifications",
        "preferred", "required", "desired", "plus", "bonus", "nice", "must",
        "duty", "duties", "day", "days", "daily", "year", "years", "month",
        "months", "week", "weeks", "time", "full", "part", "salary", "benefit",
        "benefits", "compensation", "package", "pay", "bonus", "equity",
        "insurance", "vacation", "pto", "holiday", "remote", "hybrid",
        "onsite", "office", "location", "based", "travel", "equal",
        "diversity", "inclusion", "veteran", "disability", "gender", "race",
        "religion", "orientation", "regardless", "without", "employment",
        "employee", "employees", "staff", "member", "members", "people",
        "person", "join", "joining", "looking", "seeking", "seek", "ideal",
        "successful", "please", "note", "may", "etc", "us", "we're", "you'll",
        "we'll", "join", "growing", "fast", "paced", "dynamic", "exciting",
        "passionate", "self", "starter", "detail", "oriented", "oriented",
        "responsible", "ensure", "ensuring", "support", "supporting",
        "provide", "providing", "level", "levels", "various", "related",
        "field", "degree", "bachelor", "bachelors", "master", "masters",
        "minimum", "least", "years", "plus", "experience", "experienced",
        "knowledge", "understanding", "familiarity", "proficiency",
        "proficient", "skill", "skills", "skilled", "expertise", "background",
        "demonstrated", "proven", "track", "record", "hands", "on",
    }
)

#: Tokens that are almost always genuine hard skills / credentials.  Matching
#: keywords get a relevance boost so "kubernetes" outranks "stakeholder".
SKILL_HINTS: frozenset[str] = frozenset(
    {
        # languages
        "python", "java", "javascript", "typescript", "go", "golang", "rust",
        "scala", "kotlin", "swift", "c", "c++", "c#", ".net", "php", "ruby",
        "perl", "r", "matlab", "sql", "pl/sql", "t-sql", "bash", "shell",
        "powershell", "vba", "solidity", "dax", "m",
        # web / app
        "react", "angular", "vue", "svelte", "next.js", "node", "node.js",
        "express", "django", "flask", "fastapi", "spring", "springboot",
        "rails", "laravel", "dotnet", "graphql", "rest", "grpc", "soap",
        "html", "css", "tailwind", "redux", "jquery",
        # data / ml
        "pandas", "numpy", "scipy", "sklearn", "scikit-learn", "pytorch",
        "tensorflow", "keras", "xgboost", "lightgbm", "huggingface", "llm",
        "nlp", "spark", "pyspark", "hadoop", "hive", "kafka", "flink",
        "airflow", "dbt", "snowflake", "databricks", "redshift", "bigquery",
        "synapse", "tableau", "powerbi", "looker", "qlik", "alteryx", "sas",
        "spss", "excel", "etl", "elt", "warehouse", "lakehouse", "mlops",
        # cloud / infra
        "aws", "azure", "gcp", "ec2", "s3", "lambda", "eks", "ecs", "rds",
        "docker", "kubernetes", "k8s", "helm", "terraform", "ansible",
        "puppet", "chef", "jenkins", "gitlab", "github", "argocd", "circleci",
        "prometheus", "grafana", "datadog", "splunk", "elk", "elasticsearch",
        "nginx", "linux", "unix", "windows", "vmware", "openshift",
        "ci/cd", "devops", "sre", "iac", "serverless", "microservices",
        # databases
        "postgresql", "postgres", "mysql", "oracle", "sqlserver", "mongodb",
        "cassandra", "dynamodb", "redis", "neo4j", "sqlite", "couchbase",
        # security / risk / audit / finance (consulting-heavy vocabularies)
        "soc", "soc2", "iso27001", "iso", "nist", "gdpr", "hipaa", "pci",
        "pci-dss", "sox", "coso", "cobit", "itgc", "sod", "grc", "iam",
        "siem", "soar", "dlp", "pentest", "vulnerability", "zerotrust",
        "cissp", "cisa", "cism", "crisc", "ccsp", "oscp", "ceh", "cpa",
        "cia", "cfe", "frm", "cfa", "acca", "ifrs", "gaap", "audit",
        "internal", "controls", "compliance", "risk", "governance",
        "forensics", "aml", "kyc", "basel", "solvency",
        # product / delivery
        "agile", "scrum", "kanban", "safe", "jira", "confluence", "servicenow",
        "sap", "salesforce", "workday", "oracle", "dynamics", "netsuite",
        "sharepoint", "figma", "git", "svn", "pmp", "prince2", "itil",
        "lean", "sixsigma", "kaizen", "roadmap", "stakeholder", "kpi", "okr",
    }
)

#: Single-token normalisations applied before stemming so that common aliases
#: collapse onto one concept.
SYNONYMS: dict[str, str] = {
    "js": "javascript",
    "javascripts": "javascript",
    "nodejs": "node",
    "node.js": "node",
    "reactjs": "react",
    "react.js": "react",
    "vuejs": "vue",
    "angularjs": "angular",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "postgres": "postgresql",
    "psql": "postgresql",
    "mssql": "sqlserver",
    "ms-sql": "sqlserver",
    "sql-server": "sqlserver",
    "scikit-learn": "sklearn",
    "power-bi": "powerbi",
    "powerbi": "powerbi",
    "pbi": "powerbi",
    "gcp": "gcp",
    "googlecloud": "gcp",
    "amazon": "aws",
    "ml": "machinelearning",
    "ai": "artificialintelligence",
    "iac": "infrastructureascode",
    "cicd": "ci/cd",
    "ci-cd": "ci/cd",
    "dotnet": ".net",
    "csharp": "c#",
    "cpp": "c++",
    "restful": "rest",
    "apis": "api",
    "dbs": "database",
    "databases": "database",
    "qa": "quality",
    "ux": "userexperience",
    "ui": "userinterface",
    "iso-27001": "iso27001",
    "soc-2": "soc2",
    "pcidss": "pci-dss",
    "sarbanes": "sox",
}


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\.?[a-z0-9][a-z0-9+#./\-]*")
_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,6}\s*)?\*{0,2}([A-Za-z][A-Za-z &/'\-]{2,60}?)\*{0,2}\s*:?\s*$")
_BULLET_RE = re.compile(r"^\s{0,8}(?:[-*+•▪●·⁃‣◦∙>]|\d{1,2}[.)])\s+(.*)$")
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{6,}\|?\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b", re.IGNORECASE)
_HTML_RE = re.compile(r"</?(?:div|span|table|tr|td|th|p|br|font|b|i|u|strong|em|h[1-6])\b[^>]*>", re.IGNORECASE)
_MULTICOL_RE = re.compile(r"\S[ ]{3,}\S[ ]{3,}\S")
_TAB_COL_RE = re.compile(r"\S\t+\S\t+\S")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[A-Za-z0-9\-_%.]+", re.IGNORECASE)
_PHONE_CANDIDATE_RE = re.compile(
    r"(?<![\w/])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{2,5}(?:[\s.\-]\d{2,5}){1,3}(?![\w/])"
)
_LOCATION_RE = re.compile(
    r"\b[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3},\s*(?:[A-Z]{2}\b|[A-Z][A-Za-z.\-']+)"
)
_REMOTE_RE = re.compile(r"\b(remote|open to relocation|willing to relocate)\b", re.IGNORECASE)

_MONTHS = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
)
_DATE_STYLES: dict[str, re.Pattern[str]] = {
    "Mon YYYY": re.compile(rf"\b{_MONTHS}\s+(?:19|20)\d{{2}}\b"),
    "MM/YYYY": re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}\b"),
    "YYYY-MM": re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])\b"),
    "MM-DD-YYYY": re.compile(r"\b(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])[/\-](?:19|20)?\d{2}\b"),
    "YYYY": re.compile(r"(?<![\w/\-])(?:19|20)\d{2}(?![\w/\-])"),
}
_DATE_RANGE_RE = re.compile(
    r"(?:19|20)\d{2}|" + _MONTHS + r"|\b(?:present|current|now|to date|ongoing)\b",
    re.IGNORECASE,
)
_RANGE_SEP_RE = re.compile(r"\s(?:-|–|—|to|through|until)\s", re.IGNORECASE)

_METRIC_RE = re.compile(
    r"(?:"
    r"\d+(?:[.,]\d+)*\s*%"                       # 35%
    r"|[$€£₹¥]\s?\d+(?:[.,]\d+)*\s*(?:[KMB]|k|m|bn|billion|million|thousand|lakh|crore)?"
    r"|\b\d+(?:[.,]\d+)*\s*(?:[KMB]\b|k\b|x\b|bps\b|fte\b|hrs?\b|hours?\b|days?\b|weeks?\b|months?\b|years?\b)"
    r"|\b\d{2,}(?:[.,]\d{3})*\b"                 # 1,200 / 45000
    r"|\b(?:doubled|tripled|quadrupled|halved)\b"
    r")",
    re.IGNORECASE,
)
_PRONOUN_RE = re.compile(r"\b(?:i|me|my|mine|we|our|ours|us|myself)\b", re.IGNORECASE)
_REFERENCES_RE = re.compile(r"references?\s+(?:available|upon|on)\b", re.IGNORECASE)

#: Characters that are safe in an ATS-parsed document beyond plain ASCII.
_ALLOWED_NON_ASCII = set("•–—’‘“”…°±€£₹¥áàâäãéèêëíìîïóòôöõúùûüçñÁÀÂÄÉÈÊÍÓÔÖÚÜÇÑ")
_DINGBAT_RE = re.compile(
    "["
    "←-⇿"  # arrows
    "⌀-⏿"  # technical
    "■-◿"  # geometric shapes (except the bullet chars handled above)
    "☀-➿"  # dingbats
    "⬀-⯿"
    "-"  # private use (icon fonts)
    "\U0001f000-\U0001faff"  # emoji
    "]"
)
_ZERO_WIDTH_RE = re.compile("[​-‏‪-‮﻿­]")


# ---------------------------------------------------------------------------
# Scoring weights.  MUST sum to 100.
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    "parseability": 20.0,
    "keyword_coverage": 25.0,
    "contact_block": 10.0,
    "experience_quality": 20.0,
    "formatting": 15.0,
    "readability": 10.0,
}
assert abs(sum(WEIGHTS.values()) - 100.0) < 1e-9, "ATS weights must sum to 100"


# ---------------------------------------------------------------------------
# Tokenisation / light stemming
# ---------------------------------------------------------------------------

_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("izations", "ize"),
    ("ization", "ize"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("ibility", "ible"),
    ("ability", "able"),
    ("ements", "e"),
    ("ement", "e"),
    ("ments", "e"),
    ("ment", "e"),
    ("ities", "ity"),
    ("ations", "ate"),
    ("ation", "ate"),
    ("ances", "ance"),
    ("ings", ""),
    ("ing", ""),
    ("edly", ""),
    ("ed", ""),
    ("ies", "y"),
    ("sses", "ss"),
    ("ers", ""),
    ("er", ""),
    ("ors", "or"),
    ("ly", ""),
    ("s", ""),
)

_IRREGULAR: dict[str, str] = {
    "led": "lead",
    "leading": "lead",
    "leads": "lead",
    "ran": "run",
    "running": "run",
    "built": "build",
    "building": "build",
    "drove": "drive",
    "driven": "drive",
    "driving": "drive",
    "grew": "grow",
    "grown": "grow",
    "growing": "grow",
    "wrote": "write",
    "written": "write",
    "writing": "write",
    "oversaw": "oversee",
    "overseen": "oversee",
    "overseeing": "oversee",
    "cut": "cut",
    "won": "win",
    "winning": "win",
    "sought": "seek",
    "taught": "teach",
    "spoke": "speak",
    "chose": "choose",
    "troubleshot": "troubleshoot",
    "analysed": "analyze",
    "analyzed": "analyze",
    "modelled": "model",
    "modeled": "model",
    "data": "data",
    "media": "media",
    "criteria": "criterion",
}

_KEEP_SHORT = {"r", "c", "go", "ai", "ml", "qa", "ux", "ui", "bi", "db", "it", "hr"}
_NO_COLLAPSE = {"ss", "ee", "oo"}


def stem(word: str) -> str:
    """Very light Porter-ish stemmer tuned for resume/JD vocabulary.

    Goal: ``managing`` / ``managed`` / ``manages`` / ``management`` /
    ``manager`` all collapse to the same key, while technology tokens such as
    ``kubernetes``, ``c++`` or ``.net`` are left intact.
    """
    w = (word or "").strip().lower()
    if not w:
        return ""
    if w in _IRREGULAR:
        w = _IRREGULAR[w]
    # Never mangle tech tokens with punctuation (c++, ci/cd, .net, node.js).
    if any(ch in w for ch in "+#/."):
        return w
    if len(w) <= 3:
        return w

    # Two passes so "engineering" -> "engineer" -> "engin" matches "engineer".
    for _ in range(2):
        before = w
        for suffix, replacement in _SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) + len(replacement) >= 3:
                w = w[: -len(suffix)] + replacement
                break
        # collapse a trailing doubled consonant: plann -> plan, controll -> control
        if len(w) > 3 and w[-1] == w[-2] and w[-1].isalpha() and w[-2:] not in _NO_COLLAPSE:
            w = w[:-1]
        # drop a silent trailing 'e' so manage/manag, deliver/delivere align
        if len(w) > 4 and w.endswith("e"):
            w = w[:-1]
        if w == before:
            break
    return w


def normalize_token(token: str) -> str:
    """Alias-collapse then stem a single token."""
    t = (token or "").strip().lower().strip(".-/")
    if not t:
        return ""
    t = SYNONYMS.get(t, t)
    return stem(t)


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Split text into lowercase surface tokens, preserving tech punctuation."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall((text or "").lower()):
        tok = raw.strip("-./")
        if not tok:
            continue
        if tok.replace(".", "").replace(",", "").isdigit():
            continue  # bare numbers are metrics, not keywords
        if len(tok) < 2 and tok not in _KEEP_SHORT:
            continue
        if not keep_stopwords and tok in STOPWORDS:
            continue
        out.append(tok)
    return out


def _stem_sets(text: str) -> tuple[set[str], set[str]]:
    """Return (unigram stems, bigram stems) for matching."""
    surface = tokenize(text, keep_stopwords=True)
    unigrams: set[str] = set()
    bigrams: set[str] = set()
    prev: str | None = None
    for tok in surface:
        if tok in STOPWORDS:
            prev = None
            continue
        norm = normalize_token(tok)
        if not norm:
            prev = None
            continue
        unigrams.add(norm)
        if prev:
            bigrams.add(f"{prev} {norm}")
        prev = norm
    return unigrams, bigrams


_JD_PRIORITY_RE = re.compile(
    r"(requirement|qualification|responsibilit|must have|you (?:will|have)|"
    r"what you.{0,10}(?:bring|need)|skills|experience with|proficien|expertise)",
    re.IGNORECASE,
)


def extract_keywords(text: str, *, top_n: int = 40) -> list[tuple[str, float]]:
    """Extract weighted keyword phrases from a job description (or resume).

    Returns ``[(display_phrase, weight), ...]`` sorted by descending weight.
    Weighting rules:

    * base weight = sqrt(frequency) so a term repeated 9x is not 9x as important
    * x2.0 when the term appears inside a requirements/qualifications block
    * x1.6 for two-word phrases (they are more specific than single tokens)
    * x1.8 when the token is a known hard skill / credential (``SKILL_HINTS``)
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines()
    priority_flags: list[bool] = []
    priority = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            priority_flags.append(priority)
            continue
        # A short line that looks like a heading toggles the priority region.
        if len(stripped) < 90 and _JD_PRIORITY_RE.search(stripped):
            priority = True
        elif len(stripped) < 60 and stripped.endswith(":") and not _JD_PRIORITY_RE.search(stripped):
            priority = False
        priority_flags.append(priority)

    freq: Counter[str] = Counter()
    prio: dict[str, bool] = {}
    display: dict[str, str] = {}

    for idx, line in enumerate(lines):
        is_priority = priority_flags[idx] if idx < len(priority_flags) else False
        surface = tokenize(line, keep_stopwords=True)
        prev_norm: str | None = None
        prev_surface: str | None = None
        for tok in surface:
            if tok in STOPWORDS:
                prev_norm = None
                prev_surface = None
                continue
            norm = normalize_token(tok)
            if not norm:
                prev_norm = None
                prev_surface = None
                continue
            freq[norm] += 1
            display.setdefault(norm, tok)
            prio[norm] = prio.get(norm, False) or is_priority
            if prev_norm:
                key = f"{prev_norm} {norm}"
                freq[key] += 1
                display.setdefault(key, f"{prev_surface} {tok}")
                prio[key] = prio.get(key, False) or is_priority
            prev_norm = norm
            prev_surface = tok

    scored: list[tuple[str, float]] = []
    for key, count in freq.items():
        if count < 1:
            continue
        is_bigram = " " in key
        # A bigram seen once is usually noise; require it to repeat unless the
        # document is short.
        if is_bigram and count < 2 and len(freq) > 60:
            continue
        weight = count ** 0.5
        if prio.get(key):
            weight *= 2.0
        if is_bigram:
            weight *= 1.6
        parts = key.split(" ")
        surface_parts = display.get(key, key).split(" ")
        if any(p in SKILL_HINTS for p in parts) or any(p in SKILL_HINTS for p in surface_parts):
            weight *= 1.8
        scored.append((key, round(weight, 4)))

    scored.sort(key=lambda kv: (-kv[1], kv[0]))

    # Drop unigrams that are fully contained in a higher-ranked bigram to keep
    # the "missing keywords" list readable.
    chosen: list[tuple[str, float]] = []
    covered_unigrams: set[str] = set()
    for key, weight in scored:
        if " " in key:
            chosen.append((display.get(key, key), weight))
            covered_unigrams.update(key.split(" "))
        elif key not in covered_unigrams:
            chosen.append((display.get(key, key), weight))
        if len(chosen) >= top_n:
            break
    return chosen


# ---------------------------------------------------------------------------
# Structure helpers
# ---------------------------------------------------------------------------

def _canonicalize_heading(raw: str) -> str | None:
    """Map a candidate heading line onto a canonical section name."""
    probe = re.sub(r"[^A-Z& ]", " ", (raw or "").upper())
    probe = re.sub(r"\s+", " ", probe).strip()
    if not probe:
        return None
    if probe in CANONICAL_SECTIONS:
        return probe
    if probe in _SECTION_ALIASES:
        return _SECTION_ALIASES[probe]
    # tolerate trailing words: "PROFESSIONAL EXPERIENCE (10 YEARS)"
    for name in CANONICAL_SECTIONS:
        if probe.startswith(name):
            return name
    for alias, name in _SECTION_ALIASES.items():
        if probe.startswith(alias) and len(probe) - len(alias) <= 18:
            return name
    return None


def split_sections(markdown: str) -> dict[str, str]:
    """Split a resume into ``{canonical_section: body_text}``.

    Text appearing before the first recognised heading is returned under the
    special key ``"HEADER"`` (name + contact block).
    """
    sections: dict[str, list[str]] = {"HEADER": []}
    current = "HEADER"
    for line in (markdown or "").splitlines():
        stripped = line.strip()
        heading: str | None = None
        if stripped and not _BULLET_RE.match(line) and not _TABLE_RE.match(line):
            match = _HEADING_RE.match(line)
            if match:
                looks_like_heading = (
                    line.lstrip().startswith("#")
                    or stripped.isupper()
                    or (stripped.startswith("**") and stripped.endswith("**"))
                    or stripped.endswith(":")
                )
                if looks_like_heading:
                    heading = _canonicalize_heading(match.group(1))
        if heading:
            current = heading
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {name: "\n".join(body).strip() for name, body in sections.items()}


def collect_bullets(markdown: str, *, sections: dict[str, str] | None = None) -> list[str]:
    """Return the text of every bullet line in the document."""
    source = markdown
    if sections is not None:
        # Only bullets that live in narrative sections count as "accomplishment"
        # bullets; skill-list bullets are inventory, not achievements.
        source = "\n".join(
            body for name, body in sections.items()
            if name in ("PROFESSIONAL EXPERIENCE", "PROJECTS")
        ) or markdown
    out: list[str] = []
    for line in (source or "").splitlines():
        match = _BULLET_RE.match(line)
        if match:
            text = match.group(1).strip()
            if text:
                out.append(text)
    return out


def _plain_text(markdown: str) -> str:
    """Strip markdown decoration for word counting / regex scanning."""
    text = markdown or ""
    text = _IMAGE_RE.sub(" ", text)
    text = _HTML_RE.sub(" ", text)
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1 \2", text)  # links -> "label url"
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)[*_](?=\w)|(?<=\w)[*_](?!\w)", "", text)
    return text


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’\-]+\b", text))


def _first_action_word(bullet: str) -> str:
    """The first alphabetic word of a bullet, ignoring bold/markup noise."""
    cleaned = re.sub(r"^[^A-Za-z]+", "", bullet.replace("**", "").replace("*", ""))
    match = re.match(r"[A-Za-z][A-Za-z'’\-]*", cleaned)
    return match.group(0).lower() if match else ""


_ACTION_VERB_STEMS = frozenset(stem(v) for v in ACTION_VERBS)


def _is_action_verb(word: str) -> bool:
    if not word:
        return False
    if word in ACTION_VERBS:
        return True
    return stem(word) in _ACTION_VERB_STEMS


# ---------------------------------------------------------------------------
# Subscore implementations
# Each returns (points, issues, advice, extras)
# ---------------------------------------------------------------------------

def _score_parseability(
    markdown: str, lines: Sequence[str], sections: dict[str, str]
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["parseability"]
    issues: list[str] = []
    advice: list[str] = []

    found = [s for s in CANONICAL_SECTIONS if sections.get(s)]
    missing_required = [s for s in REQUIRED_SECTIONS if not sections.get(s)]
    # 8 pts: required headings present and populated
    heading_pts = 8.0 * (len(REQUIRED_SECTIONS) - len(missing_required)) / len(REQUIRED_SECTIONS)
    if missing_required:
        issues.append("Missing or empty required section(s): " + ", ".join(missing_required))
        advice.append(
            "Add explicit headings for " + ", ".join(missing_required)
            + " -- parsers key off literal section names."
        )

    # 3 pts: no markdown/pipe tables
    table_lines = [ln for ln in lines if _TABLE_RE.match(ln) and not _TABLE_SEP_RE.match(ln)]
    table_pts = 3.0 if not table_lines else 0.0
    if table_lines:
        issues.append(f"{len(table_lines)} table row(s) detected; many parsers scramble table cells.")
        advice.append("Replace tables with plain 'Label: value' lines or bullets.")

    # 3 pts: no images / embedded HTML
    graphic_pts = 3.0
    if _IMAGE_RE.search(markdown):
        graphic_pts -= 2.0
        issues.append("Image/graphic reference found; ATS parsers discard images entirely.")
        advice.append("Remove photos, logos and icon graphics from the resume.")
    if _HTML_RE.search(markdown):
        graphic_pts -= 1.0
        issues.append("Raw HTML tags found in the resume body.")
    graphic_pts = max(0.0, graphic_pts)

    # 3 pts: no multi-column artifacts
    col_lines = [ln for ln in lines if _MULTICOL_RE.search(ln) or _TAB_COL_RE.search(ln)]
    column_pts = 3.0 if not col_lines else max(0.0, 3.0 - len(col_lines) * 0.5)
    if col_lines:
        issues.append(
            f"{len(col_lines)} line(s) look like side-by-side columns (wide gaps / tabs)."
        )
        advice.append("Keep a strict single-column layout; never simulate columns with spaces or tabs.")

    # 3 pts: sane line lengths, no giant single-line blob
    long_lines = [ln for ln in lines if len(ln) > 220]
    unbroken = len(lines) <= 3 and _word_count(markdown) > 200
    length_pts = 3.0
    if long_lines:
        length_pts -= min(2.0, len(long_lines) * 0.5)
        issues.append(f"{len(long_lines)} very long line(s) (>220 chars) may wrap unpredictably.")
    if unbroken:
        length_pts = 0.0
        issues.append("Document has almost no line breaks; structure is unrecoverable for a parser.")
    length_pts = max(0.0, length_pts)

    total = min(max_pts, heading_pts + table_pts + graphic_pts + column_pts + length_pts)
    extras = {
        "sections_found": found,
        "sections_missing_required": missing_required,
        "sections_missing_optional": [s for s in OPTIONAL_SECTIONS if not sections.get(s)],
        "table_rows": len(table_lines),
        "column_artifact_lines": len(col_lines),
    }
    return round(total, 2), issues, advice, extras


def _score_keywords(
    markdown: str, sections: dict[str, str], job_description: str | None
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["keyword_coverage"]
    issues: list[str] = []
    advice: list[str] = []

    resume_unigrams, resume_bigrams = _stem_sets(_plain_text(markdown))

    if job_description and job_description.strip():
        keywords = extract_keywords(job_description, top_n=40)
        source = "job_description"
    else:
        # No JD available: fall back to a self-consistency check -- are the
        # skills the candidate claims actually evidenced in the experience
        # bullets?  This is a weaker signal, so we say so in `issues`.
        claimed = " \n".join(
            filter(None, [sections.get("TECHNICAL SKILLS", ""), sections.get("CORE COMPETENCIES", "")])
        )
        keywords = extract_keywords(claimed, top_n=25)
        source = "resume_skills_self_consistency"
        issues.append(
            "No job description supplied: keyword coverage measured against the resume's own "
            "skills sections (self-consistency), which is a weaker proxy."
        )
        advice.append("Score against a specific job description for a meaningful keyword read-out.")
        if keywords:
            # Match claimed skills against the narrative sections only.
            narrative = "\n".join(
                filter(None, [
                    sections.get("PROFESSIONAL SUMMARY", ""),
                    sections.get("PROFESSIONAL EXPERIENCE", ""),
                    sections.get("PROJECTS", ""),
                ])
            )
            resume_unigrams, resume_bigrams = _stem_sets(_plain_text(narrative))

    if not keywords:
        issues.append("Could not extract keywords (no job description and no skills sections).")
        return round(max_pts * 0.4, 2), issues, advice, {
            "source": source, "matched": [], "missing": [], "coverage": 0.0,
        }

    matched: list[str] = []
    partial: list[str] = []
    missing: list[str] = []
    total_weight = 0.0
    got_weight = 0.0

    for phrase, weight in keywords:
        total_weight += weight
        norm_parts = [normalize_token(p) for p in phrase.split(" ") if normalize_token(p)]
        if not norm_parts:
            continue
        if len(norm_parts) == 1:
            if norm_parts[0] in resume_unigrams:
                matched.append(phrase)
                got_weight += weight
            else:
                missing.append(phrase)
        else:
            key = " ".join(norm_parts)
            if key in resume_bigrams:
                matched.append(phrase)
                got_weight += weight
            elif all(p in resume_unigrams for p in norm_parts):
                # Both concepts present but not adjacent -> half credit.
                partial.append(phrase)
                got_weight += weight * 0.5
            else:
                missing.append(phrase)

    coverage = (got_weight / total_weight) if total_weight else 0.0
    # 75% weighted coverage is treated as an excellent, non-stuffed match.
    points = max_pts * min(1.0, coverage / 0.75)

    if coverage < 0.4 and source == "job_description":
        issues.append(f"Only {coverage * 100:.0f}% weighted keyword coverage against the job description.")
    if missing:
        top_missing = missing[:12]
        advice.append(
            "Work these job-description terms into your bullets where they are TRUE for you: "
            + ", ".join(top_missing)
        )

    # Keyword-stuffing guard: a term repeated absurdly often reads as spam to
    # humans and is discounted by modern parsers.
    plain_lower = _plain_text(markdown).lower()
    stuffed = []
    for phrase, _w in keywords[:20]:
        count = plain_lower.count(phrase.lower())
        if count >= 8:
            stuffed.append(f"{phrase} ({count}x)")
    if stuffed:
        points *= 0.9
        issues.append("Possible keyword stuffing: " + ", ".join(stuffed))
        advice.append("Mention each key term 2-4 times in real context rather than repeating it.")

    extras = {
        "source": source,
        "coverage": round(coverage, 4),
        "keyword_count": len(keywords),
        "partial_matches": partial,
        "stuffed": stuffed,
    }
    return round(min(max_pts, points), 2), issues, advice, extras | {"matched": matched, "missing": missing}


def _find_phone(text: str) -> str | None:
    """Find a plausible phone number (10-15 digits), ignoring dates/ranges."""
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        if not (9 <= len(digits) <= 15):
            continue
        # Reject year ranges like "2019 - 2022" and pure 4+4 digit splits.
        if re.fullmatch(r"(?:19|20)\d{2}[\s.\-](?:19|20)\d{2}", candidate.strip()):
            continue
        if len(digits) == 8 and re.match(r"^(?:19|20)", digits):
            continue
        return candidate.strip()
    return None


def _score_contact(
    markdown: str, sections: dict[str, str]
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["contact_block"]
    issues: list[str] = []
    advice: list[str] = []

    header = sections.get("HEADER", "")
    # Contact details are supposed to live at the top, but accept them anywhere
    # (and only warn if they are buried).
    scan_header = _plain_text(header)
    scan_all = _plain_text(markdown)

    email = _EMAIL_RE.search(scan_header) or _EMAIL_RE.search(scan_all)
    phone = _find_phone(scan_header) or _find_phone(scan_all)
    linkedin = _LINKEDIN_RE.search(scan_header) or _LINKEDIN_RE.search(scan_all)
    location_match = _LOCATION_RE.search(scan_header) or _REMOTE_RE.search(scan_header)

    points = 0.0
    if email:
        points += 3.0
    else:
        issues.append("No email address found.")
        advice.append("Put a professional email address on its own line in the header.")
    if phone:
        points += 2.5
    else:
        issues.append("No phone number found.")
        advice.append("Add a phone number with country code, e.g. +91 98765 43210.")
    if location_match:
        points += 2.0
    else:
        issues.append("No city/location line found in the header.")
        advice.append("Add 'City, Country' (or 'Remote') -- recruiters filter by location.")
    if linkedin:
        points += 2.5
    else:
        issues.append("No LinkedIn profile URL found.")
        advice.append("Add your full linkedin.com/in/... URL as plain text.")

    if (email or phone) and not (_EMAIL_RE.search(scan_header) or _find_phone(scan_header)):
        issues.append("Contact details are not in the top header block; parsers may miss them.")

    extras = {
        "email": email.group(0) if email else None,
        "phone": phone,
        "linkedin": linkedin.group(0) if linkedin else None,
        "location_detected": bool(location_match),
    }
    return round(min(max_pts, points), 2), issues, advice, extras


def _score_experience(
    markdown: str, sections: dict[str, str], bullets: Sequence[str]
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["experience_quality"]
    issues: list[str] = []
    advice: list[str] = []

    if not bullets:
        issues.append("No bullet points found in the experience section.")
        advice.append("Describe each role with 3-6 bullets; avoid paragraph blobs.")
        return 0.0, issues, advice, {
            "bullet_count": 0, "action_verb_pct": 0.0, "quantified_pct": 0.0,
            "weak_bullets": [], "unquantified_bullets": [],
        }

    verb_ok: list[str] = []
    verb_bad: list[str] = []
    quantified: list[str] = []
    unquantified: list[str] = []

    for bullet in bullets:
        word = _first_action_word(bullet)
        if _is_action_verb(word):
            verb_ok.append(bullet)
        else:
            verb_bad.append(bullet)
        if _METRIC_RE.search(bullet):
            quantified.append(bullet)
        else:
            unquantified.append(bullet)

    verb_pct = len(verb_ok) / len(bullets)
    quant_pct = len(quantified) / len(bullets)

    # 10 pts: action-verb openers (>=85% earns full marks)
    verb_pts = 10.0 * min(1.0, verb_pct / 0.85)
    # 7 pts: quantified bullets (>=45% earns full marks -- demanding 100% invites fabrication)
    quant_pts = 7.0 * min(1.0, quant_pct / 0.45)
    # 3 pts: enough bullets to be credible, not so many it is a wall
    count = len(bullets)
    if 8 <= count <= 28:
        count_pts = 3.0
    elif 5 <= count < 8 or 28 < count <= 36:
        count_pts = 2.0
    elif count < 5:
        count_pts = 1.0
    else:
        count_pts = 1.0

    if verb_pct < 0.85:
        issues.append(
            f"{len(verb_bad)} of {count} bullets do not start with a strong action verb "
            f"({verb_pct * 100:.0f}% compliant)."
        )
        advice.append(
            "Rewrite bullets to open with a past-tense action verb (Led, Automated, Reduced...) "
            "and drop openers like 'Responsible for' or 'Worked on'."
        )
    if quant_pct < 0.45:
        issues.append(f"Only {quant_pct * 100:.0f}% of bullets contain a number or measurable result.")
        advice.append(
            "Quantify outcomes you can actually verify (%, currency, volume, time saved). "
            "Never invent a metric -- state the achievement plainly instead."
        )
    if count < 5:
        issues.append("Very few accomplishment bullets; the experience section looks thin.")
    if count > 36:
        issues.append("Very many bullets; prioritise the strongest and cut duty-list filler.")

    if _PRONOUN_RE.search("\n".join(bullets)):
        issues.append("First-person pronouns found in bullets ('I', 'my', 'we').")
        advice.append("Drop pronouns: 'Led a team of 6', not 'I led a team of 6'.")
    if _REFERENCES_RE.search(_plain_text(markdown)):
        issues.append("'References available upon request' is dead weight in a modern resume.")
        advice.append("Delete the references line and use the space for an accomplishment.")

    total = min(max_pts, verb_pts + quant_pts + count_pts)
    extras = {
        "bullet_count": count,
        "action_verb_pct": round(verb_pct * 100, 1),
        "quantified_pct": round(quant_pct * 100, 1),
        "weak_bullets": verb_bad[:8],
        "unquantified_bullets": unquantified[:8],
    }
    return round(total, 2), issues, advice, extras


def _score_formatting(
    markdown: str, lines: Sequence[str], sections: dict[str, str]
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["formatting"]
    issues: list[str] = []
    advice: list[str] = []
    plain = _plain_text(markdown)

    # --- 4 pts: consistent date formats -------------------------------------
    date_lines = [ln for ln in lines if _DATE_RANGE_RE.search(ln) and _RANGE_SEP_RE.search(ln)]
    styles: Counter[str] = Counter()
    for line in date_lines:
        for name, pattern in _DATE_STYLES.items():
            if name == "YYYY":
                continue  # bare years are counted only if nothing else matched
            if pattern.search(line):
                styles[name] += 1
                break
        else:
            if _DATE_STYLES["YYYY"].search(line):
                styles["YYYY"] += 1
    distinct = len(styles)
    if not date_lines:
        date_pts = 1.0
        issues.append("No employment date ranges detected; parsers use dates to compute tenure.")
        advice.append("Add 'Mon YYYY - Mon YYYY' (or 'Present') to every role.")
    elif distinct <= 1:
        date_pts = 4.0
    elif distinct == 2:
        date_pts = 2.0
        issues.append("Two different date formats in use: " + ", ".join(sorted(styles)))
        advice.append("Pick one date format (recommended: 'Mar 2021 - Jun 2024') and use it everywhere.")
    else:
        date_pts = 0.5
        issues.append("Three or more date formats in use: " + ", ".join(sorted(styles)))
        advice.append("Normalise every date to a single 'Mon YYYY' format.")

    # --- 3 pts: consistent bullet character ---------------------------------
    bullet_chars: Counter[str] = Counter()
    for line in lines:
        match = re.match(r"^\s{0,8}([-*+•▪●·⁃‣◦∙>])\s+\S", line)
        if match:
            bullet_chars[match.group(1)] += 1
    if not bullet_chars:
        bullet_pts = 1.0
    elif len(bullet_chars) == 1:
        bullet_pts = 3.0
    else:
        bullet_pts = max(0.0, 3.0 - (len(bullet_chars) - 1) * 1.5)
        issues.append(
            "Mixed bullet characters: " + ", ".join(f"{c!r}x{n}" for c, n in bullet_chars.items())
        )
        advice.append("Use one bullet character throughout (Hermes renders '-' as a plain bullet).")

    # --- 3 pts: no exotic glyphs -------------------------------------------
    glyph_pts = 3.0
    dingbats = set(_DINGBAT_RE.findall(markdown or ""))
    # The standard bullet/dash set is explicitly allowed.
    dingbats -= _ALLOWED_NON_ASCII
    zero_width = _ZERO_WIDTH_RE.findall(markdown or "")
    odd_non_ascii = {
        ch for ch in set(markdown or "")
        if ord(ch) > 127 and ch not in _ALLOWED_NON_ASCII and not _DINGBAT_RE.match(ch)
    }
    if dingbats:
        glyph_pts -= 2.0
        issues.append("Decorative glyphs/emoji/icons found: " + " ".join(sorted(dingbats))[:60])
        advice.append("Remove icon fonts and emoji; they become garbage characters after parsing.")
    if zero_width:
        glyph_pts -= 1.0
        issues.append(f"{len(zero_width)} invisible/zero-width character(s) found.")
        advice.append("Strip zero-width and soft-hyphen characters (usually pasted from the web).")
    if odd_non_ascii and not dingbats:
        # Accented letters are fine; only flag if there are a lot of oddities.
        if len(odd_non_ascii) > 6:
            glyph_pts -= 0.5
            issues.append("Several unusual non-ASCII characters found; verify they render correctly.")
    if "\t" in (markdown or ""):
        glyph_pts -= 0.5
        issues.append("Tab characters found; use plain line breaks instead.")
    glyph_pts = max(0.0, glyph_pts)

    # --- 5 pts: length (approx 1-2 pages) ----------------------------------
    words = _word_count(plain)
    if 400 <= words <= 1000:
        length_pts = 5.0
    elif 320 <= words < 400 or 1000 < words <= 1200:
        length_pts = 3.5
    elif 220 <= words < 320 or 1200 < words <= 1500:
        length_pts = 2.0
    else:
        length_pts = 0.5
    if words < 320:
        issues.append(f"Resume is short ({words} words); it will read as thin for a mid/senior role.")
        advice.append("Aim for 400-1000 words (roughly one to two pages).")
    elif words > 1200:
        issues.append(f"Resume is long ({words} words); likely over two pages.")
        advice.append("Trim to 400-1000 words by cutting older roles and duty-list bullets.")

    # ALL-CAPS body text and heading-case checks
    caps_lines = [
        ln for ln in lines
        if len(ln.strip()) > 40 and ln.strip().isupper()
    ]
    if caps_lines:
        issues.append(f"{len(caps_lines)} long ALL-CAPS line(s) in the body; reserve caps for headings.")

    total = min(max_pts, date_pts + bullet_pts + glyph_pts + length_pts)
    extras = {
        "word_count": words,
        "date_styles": dict(styles),
        "bullet_chars": {repr(c): n for c, n in bullet_chars.items()},
        "estimated_pages": round(words / 500.0, 2) if words else 0.0,
    }
    return round(total, 2), issues, advice, extras


def _score_readability(
    sections: dict[str, str], bullets: Sequence[str]
) -> tuple[float, list[str], list[str], dict[str, Any]]:
    max_pts = WEIGHTS["readability"]
    issues: list[str] = []
    advice: list[str] = []

    # --- 6 pts: average bullet length in the 12-28 word sweet spot ---------
    if bullets:
        lengths = [_word_count(b) for b in bullets]
        avg = sum(lengths) / len(lengths)
        if 12 <= avg <= 28:
            bullet_pts = 6.0
        elif 9 <= avg < 12 or 28 < avg <= 34:
            bullet_pts = 4.0
        elif 6 <= avg < 9 or 34 < avg <= 42:
            bullet_pts = 2.0
        else:
            bullet_pts = 0.5
        if avg < 12:
            issues.append(f"Bullets average {avg:.0f} words; too terse to convey scope or impact.")
            advice.append("Expand bullets to 12-28 words: action + what + measurable result.")
        elif avg > 28:
            issues.append(f"Bullets average {avg:.0f} words; they read as paragraphs.")
            advice.append("Split long bullets so each makes one point in under ~28 words.")
        over_long = [b for b, n in zip(bullets, lengths) if n > 45]
        if over_long:
            bullet_pts = max(0.0, bullet_pts - 1.0)
            issues.append(f"{len(over_long)} bullet(s) exceed 45 words.")
    else:
        avg = 0.0
        bullet_pts = 0.0
        over_long = []
        issues.append("No bullets to assess for readability.")

    # --- 4 pts: no wall-of-text paragraphs inside experience ---------------
    experience_body = "\n".join(
        filter(None, [sections.get("PROFESSIONAL EXPERIENCE", ""), sections.get("PROJECTS", "")])
    )
    fat_paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", experience_body):
        block = block.strip()
        if not block or _BULLET_RE.match(block.splitlines()[0]):
            continue
        # ignore company/title/date header lines
        if len(block.splitlines()) == 1 and _word_count(block) < 20:
            continue
        if _word_count(block) > 60:
            fat_paragraphs.append(block[:160])
    if not fat_paragraphs:
        para_pts = 4.0
    else:
        para_pts = max(0.0, 4.0 - len(fat_paragraphs) * 1.5)
        issues.append(f"{len(fat_paragraphs)} paragraph(s) over 60 words inside the experience section.")
        advice.append("Convert dense paragraphs into 3-6 scannable bullets per role.")

    summary_words = _word_count(sections.get("PROFESSIONAL SUMMARY", ""))
    if summary_words and summary_words > 120:
        issues.append(f"Professional summary is {summary_words} words; keep it under ~90.")
        advice.append("Cut the summary to 3-4 lines of positioning, then let bullets prove it.")

    total = min(max_pts, bullet_pts + para_pts)
    extras = {
        "avg_words_per_bullet": round(avg, 1),
        "long_bullets": len(over_long),
        "fat_paragraphs": len(fat_paragraphs),
        "summary_words": summary_words,
    }
    return round(total, 2), issues, advice, extras


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_resume_deterministic(markdown: str, job_description: str | None) -> dict:
    """Score a resume with pure Python -- no LLM, no network, no I/O.

    HEURISTIC PROXY, NOT A VENDOR SCORE.  This measures the document
    properties that mainstream ATS parsers and recruiter keyword searches
    demonstrably depend on (section recognisability, keyword overlap with the
    target job description, machine-extractable contact details, quantified
    action bullets, boring consistent formatting, human scannability).  It
    cannot and does not reproduce the score of Workday, Greenhouse, Taleo,
    iCIMS, Lever or any other specific engine -- those are proprietary,
    recruiter-configured and not publicly specified.  Use the ``issues`` and
    ``advice`` lists as the actionable output.

    Parameters
    ----------
    markdown:
        The resume in the canonical Hermes markdown form (also tolerates
        plain-text resumes and common third-party heading synonyms).
    job_description:
        Target JD text.  When ``None``/empty, keyword coverage degrades to a
        self-consistency check of the resume's claimed skills against its own
        experience narrative, and that is reported in ``issues``.

    Returns
    -------
    dict with keys: ``score`` (0-100 float), ``subscores`` (name -> float),
    ``max_subscores``, ``weights``, ``matched``, ``missing``, ``issues``,
    ``advice``, ``details``, ``is_heuristic`` (always True).
    """
    text = markdown or ""
    lines = text.splitlines()
    sections = split_sections(text)
    bullets = collect_bullets(text, sections=sections)

    issues: list[str] = []
    advice: list[str] = []
    details: dict[str, Any] = {}
    subscores: dict[str, float] = {}

    if not text.strip():
        return {
            "score": 0.0,
            "subscores": {k: 0.0 for k in WEIGHTS},
            "max_subscores": dict(WEIGHTS),
            "weights": dict(WEIGHTS),
            "matched": [],
            "missing": [],
            "issues": ["Resume is empty."],
            "advice": ["Generate or upload a resume first."],
            "details": {},
            "is_heuristic": True,
        }

    pts, iss, adv, extra = _score_parseability(text, lines, sections)
    subscores["parseability"] = pts
    issues += iss
    advice += adv
    details["parseability"] = extra

    pts, iss, adv, extra = _score_keywords(text, sections, job_description)
    subscores["keyword_coverage"] = pts
    issues += iss
    advice += adv
    matched = extra.pop("matched", [])
    missing = extra.pop("missing", [])
    details["keyword_coverage"] = extra

    pts, iss, adv, extra = _score_contact(text, sections)
    subscores["contact_block"] = pts
    issues += iss
    advice += adv
    details["contact_block"] = extra

    pts, iss, adv, extra = _score_experience(text, sections, bullets)
    subscores["experience_quality"] = pts
    issues += iss
    advice += adv
    details["experience_quality"] = extra

    pts, iss, adv, extra = _score_formatting(text, lines, sections)
    subscores["formatting"] = pts
    issues += iss
    advice += adv
    details["formatting"] = extra

    pts, iss, adv, extra = _score_readability(sections, bullets)
    subscores["readability"] = pts
    issues += iss
    advice += adv
    details["readability"] = extra

    score = round(sum(subscores.values()), 1)
    details["section_word_counts"] = {
        name: _word_count(body) for name, body in sections.items() if body
    }

    return {
        "score": score,
        "subscores": {k: round(v, 2) for k, v in subscores.items()},
        "max_subscores": dict(WEIGHTS),
        "weights": dict(WEIGHTS),
        "matched": matched,
        "missing": missing,
        "issues": issues,
        "advice": advice,
        "details": details,
        "is_heuristic": True,
    }


_SEMANTIC_SYSTEM = """You are a blunt, senior technical recruiter reviewing a resume against a job description.
You judge SEMANTIC fit -- whether the candidate's actual demonstrated experience would survive a
recruiter screen and a hiring-manager read -- not formatting (a separate deterministic checker owns that).

Rules:
- Judge only what the resume states. Do not assume unstated experience.
- Penalise duty-list bullets, vague ownership claims, and seniority/domain mismatch.
- Reward specific, verifiable scope: systems named, scale, measurable outcomes, relevant domain.
- Be concrete and actionable. No praise, no hedging, no restating the resume.
- Output ONLY the requested JSON object."""

_SEMANTIC_SCHEMA = """{
  "semantic_fit": 0-100 integer (how well the demonstrated experience matches the target role),
  "llm_issues": ["specific weaknesses a recruiter would notice, max 8"],
  "llm_advice": ["specific, concrete rewrite/positioning actions, max 8"]
}"""


async def score_resume(
    llm: Any,
    markdown: str,
    job_description: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Deterministic score + an LLM semantic-fit pass, blended.

    The returned dict is the deterministic result plus:
    ``semantic_fit`` (0-100 or ``None`` if the LLM was unavailable),
    ``llm_issues``, ``llm_advice``, ``deterministic_score`` and a re-blended
    ``score`` = 75% deterministic + 25% semantic.

    The blend is deliberately deterministic-heavy: the mechanical checks are
    reproducible, while the LLM number is a judgement call that varies between
    models.  If the LLM call fails for any reason the deterministic result is
    returned unchanged (with ``semantic_fit = None``) -- scoring must never be
    a hard dependency on the router being up.
    """
    result = score_resume_deterministic(markdown, job_description)
    result["deterministic_score"] = result["score"]
    result["semantic_fit"] = None
    result["llm_issues"] = []
    result["llm_advice"] = []

    async def _emit(message: str, level: str = "info") -> None:
        if not run_id:
            return
        try:
            from hermes.events import bus  # local import: keeps this module LLM/IO-free

            outcome = bus.publish(run_id, level, f"[ats] {message}")
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception as exc:  # pragma: no cover - telemetry is best-effort
            log.debug("ats emit failed: %s", exc)

    await _emit(
        f"Deterministic ATS score {result['score']:.1f}/100 "
        f"({len(result['issues'])} issues, {len(result['missing'])} missing keywords)"
    )

    if llm is None or not (markdown or "").strip():
        result["llm_issues"] = ["LLM semantic pass skipped (no router configured)."]
        return result

    jd_block = (
        f"TARGET JOB DESCRIPTION:\n{(job_description or '')[:6000]}"
        if job_description and job_description.strip()
        else "TARGET JOB DESCRIPTION: (none supplied -- judge general market readiness "
             "for the roles the resume targets)"
    )
    prompt = (
        f"{jd_block}\n\n"
        f"RESUME (markdown):\n{(markdown or '')[:14000]}\n\n"
        f"Deterministic checker already found these mechanical issues (do NOT repeat them):\n"
        f"{chr(10).join('- ' + i for i in result['issues'][:10]) or '- none'}\n\n"
        f"Return ONLY this JSON:\n{_SEMANTIC_SCHEMA}"
    )

    try:
        await _emit("Running LLM semantic-fit pass")
        data = await llm.chat_json(
            [
                {"role": "system", "content": _SEMANTIC_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            schema_hint=_SEMANTIC_SCHEMA,
            temperature=0.15,
            json_mode=True,
            run_id=run_id,
        )
        if not isinstance(data, dict):
            data = {}
    except Exception as exc:
        log.warning("LLM semantic ATS pass failed: %s", exc)
        await _emit(f"Semantic pass unavailable ({type(exc).__name__}); returning deterministic score only", "warn")
        result["llm_issues"] = [f"LLM semantic pass failed: {type(exc).__name__}"]
        return result

    raw_fit = data.get("semantic_fit", data.get("score"))
    try:
        semantic = max(0.0, min(100.0, float(raw_fit)))
    except (TypeError, ValueError):
        semantic = None

    def _as_str_list(value: Any, limit: int = 8) -> list[str]:
        items: list[str] = []
        if isinstance(value, str):
            value = [value]
        for item in value if isinstance(value, (list, tuple)) else []:
            if isinstance(item, dict):
                item = item.get("text") or item.get("issue") or item.get("advice") or ""
            text = re.sub(r"\s+", " ", str(item)).strip()
            if text:
                items.append(text[:400])
            if len(items) >= limit:
                break
        return items

    result["llm_issues"] = _as_str_list(data.get("llm_issues") or data.get("issues"))
    result["llm_advice"] = _as_str_list(data.get("llm_advice") or data.get("advice"))

    if semantic is not None:
        result["semantic_fit"] = round(semantic, 1)
        result["score"] = round(0.75 * float(result["deterministic_score"]) + 0.25 * semantic, 1)
        await _emit(
            f"Blended ATS score {result['score']:.1f}/100 "
            f"(deterministic {result['deterministic_score']:.1f}, semantic {semantic:.0f})"
        )
    else:
        result["llm_issues"].append("Model did not return a usable semantic_fit number.")

    return result
