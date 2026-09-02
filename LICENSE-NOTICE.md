# Third-Party Notices and Terms-of-Service Warning

Hermes is a self-hosted assembly of software. Two of its five containers are
third-party images that Hermes pulls but does not vendor, modify, or
redistribute. This file records what they are, who wrote them, and — more
importantly — what you are agreeing to when you run them.

**Read the "LinkedIn Terms of Service" section before you run `make up`.** It is
not boilerplate.

---

## 1. Third-party components

### freellmapi — OpenAI-compatible free-tier LLM router

| | |
|---|---|
| **Author** | Tashfeen Ahmed (`tashfeenahmed`) |
| **Source** | https://github.com/tashfeenahmed/freellmapi |
| **Image** | `ghcr.io/tashfeenahmed/freellmapi:latest` |
| **Role in Hermes** | Every LLM call from `hermes-core` goes through this router. Hermes holds no upstream provider keys itself. |
| **License** | See the `LICENSE` file in the upstream repository. Hermes redistributes no part of it. |

Notes and obligations that pass through to you:

- The router aggregates the **free tiers of commercial LLM providers**. Those
  providers' own terms of service, rate limits, and acceptable-use policies
  apply to every request Hermes makes on your behalf. Sending your resume and
  employment history through a free tier commonly means the provider may retain
  or train on that content. Check the policy of each provider you add.
- Your resume, LinkedIn profile text, and the job descriptions Hermes scrapes
  are all sent to whichever upstream model you configure. **If that is not
  acceptable to you, point `FREELLMAPI_BASE_URL` at a local model instead** —
  the router can reach a host-local Ollama or LM Studio via
  `host.docker.internal`, and Hermes never needs to know the difference.
- The image is pinned to the `latest` tag by upstream convention. Pin it to a
  digest if you need reproducible builds.

### linkedin-mcp-server — LinkedIn scraping tools over MCP

| | |
|---|---|
| **Author** | Daniel Stickel (`stickerdaniel`) |
| **Source** | https://github.com/stickerdaniel/linkedin-mcp-server |
| **Image** | `stickerdaniel/linkedin-mcp-server:4.23.2` (pinned) |
| **Role in Hermes** | Reads your own profile, searches jobs, fetches job details, lists saved jobs. |
| **License** | See the `LICENSE` file in the upstream repository. Hermes redistributes no part of it. |

It bundles further third-party software of its own, including a Playwright
browser runtime and Chromium. Their licenses apply within that image.

### Everything else

`hermes-core`, `hermes-dashboard`, and `hermes-sandbox` are first-party code in
this repository. Their Python and JavaScript dependencies are declared in
`services/core/requirements.txt`, `services/sandbox/Dockerfile` (the sandbox
has a single pinned dependency, installed inline), and
`services/dashboard/package.json`; each carries its own license.

The sandbox image includes **LibreOffice** (used headlessly for
Markdown → DOCX → PDF conversion), which is licensed under the MPL-2.0.

---

## 2. LinkedIn Terms of Service — plain-language warning

**Running an automated client against LinkedIn can violate the LinkedIn User
Agreement, and carries a real, non-theoretical risk that your account is
restricted or permanently banned.**

LinkedIn's User Agreement and its `robots.txt` prohibit, among other things,
scraping, using bots or automated methods to access the service, copying
profiles or data without permission, and using software that circumvents the
platform's technical limits. The `linkedin-mcp` container drives a real logged-in
browser session using **your** cookies. From LinkedIn's side, that traffic is
attributable to you and to nobody else.

What that means concretely:

- **The account at risk is yours.** Consequences range from a temporary
  read-only restriction, through a forced password reset and identity
  verification, to permanent account termination. Recovering a restricted
  LinkedIn account is slow and frequently unsuccessful.
- **The risk scales with volume.** Large `max_pages` values, tight scheduling,
  and long unbroken scraping sessions are the patterns that get flagged.
  Occasional, human-paced use is far less conspicuous than a nightly crawl.
- **Consider a secondary account and you may lose that too** — and LinkedIn's
  terms also prohibit maintaining multiple accounts, so this is not a clean
  workaround.
- **No warranty.** Hermes' authors and the authors of the third-party images
  above make no representation that this software's use complies with LinkedIn's
  terms, and accept no liability for any action LinkedIn takes against your
  account. You run it at your own risk, on your own infrastructure, under your
  own credentials.

This is not legal advice. If the stakes matter to you — your job search
genuinely depends on this account — read the current LinkedIn User Agreement
yourself and decide accordingly. The safest configuration of Hermes is one where
you paste your profile and target job descriptions in by hand and never start
the `linkedin-mcp` container at all; the resume builder and the ATS scorer work
fine that way.

### Design consequence: Hermes never auto-submits an application

This is the single most important design decision in the project, and it follows
directly from the section above.

- **There is no apply tool.** The upstream MCP server exposes none, and Hermes
  adds none. It does not fill application forms, click Submit, send Easy Apply
  requests, or message recruiters.
- **What Hermes actually produces** is a ranked list of jobs, a per-job match
  breakdown, an ATS-scored resume tailored to a specific posting, and a plain
  `https://www.linkedin.com/jobs/view/<id>/` link.
- **You click the link. You read the posting. You press Submit.** A human being
  reviews and sends every application, without exception.

That boundary exists for three reasons, all of which stand on their own:

1. **It is the highest-risk automation on the platform.** Mass automated
   applications are exactly what LinkedIn's abuse detection is tuned to catch,
   and the account it burns is yours.
2. **It is bad for you.** Applications submitted without a human reading the
   posting are low-quality applications. Employers notice, and so do the
   applicant-tracking systems that deduplicate spray-and-pray applicants.
3. **It keeps you accountable for your own claims.** Everything in a Hermes
   resume must be traceable to your real profile — the resume agent is
   instructed never to invent an employer, a date, a degree, or a metric. You
   are the one signing your name to it, so you are the one who has to read it
   first.

If you were hoping for a bot that applies to 400 jobs overnight, Hermes is not
that, will not become that, and the refusal is deliberate.

### On ATS scores

Hermes' ATS score is a **documented heuristic**, not a reading from any real
applicant-tracking system. It checks parseability, keyword coverage against the
actual job description, contact-block completeness, bullet quality, formatting
consistency, and readability, and it explains every subscore it gives. Real
vendors (Workday, Greenhouse, Taleo, iCIMS, Lever) each parse differently and
none of them publish a score. Treat a high Hermes score as "this resume is
clean, parseable, and on-topic" — which is worth a lot — and not as a
prediction about any specific employer's pipeline.

---

## 3. Operational security notes

- **`/var/run/docker.sock` is bind-mounted into `hermes-core`** so it can spawn
  hardened, ephemeral sandbox containers and drive the Containers dashboard
  page. Access to the Docker socket is effectively root on the host machine.
  The extended comment above that mount in `docker-compose.yml` lists the
  mitigations. Do not expose this stack to a network you do not control.
- **Every port binds to `127.0.0.1` by default** (`HOST_BIND`). There is no
  authentication in front of any Hermes service. Changing `HOST_BIND` to
  `0.0.0.0` publishes your LinkedIn session, your resume, and an LLM router
  holding provider keys to everyone who can reach that interface.
- **`make backup` produces a file containing your LinkedIn session cookies.**
  Anyone who holds that archive can resume your logged-in session. Treat it
  like a password database.
- **Hermes never asks for, types, or stores your LinkedIn password.** Login
  happens in the one-shot `linkedin-login` container, in a browser you drive
  yourself through the noVNC viewer on port 6080. Only the resulting session
  state is persisted, in the `linkedin-session` volume.
