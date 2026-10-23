# Devpost submission — Life After Code (GitLab Transcend Hackathon)

Project name: **SentinelGraph — Life After Code**
Tagline: *A bank-grade fraud investigator whose post-code lifecycle is run by GitLab Duo agents. Humans approve; agents do the rest.*

Path: **B — Bring Your Own** · Autonomy: **Supervised** · Lifecycle stages touched: **9 of 9**

- GitLab project: https://gitlab.com/nekku-solo/sentinelgraph-client
- Original MIT project: https://github.com/nirmaljosephukken/sentinelgraph-client
- Pipeline history: https://gitlab.com/nekku-solo/sentinelgraph-client/-/pipelines
- Agent sessions: https://gitlab.com/nekku-solo/sentinelgraph-client/-/automate/agent-sessions
- Live demo of the agents at work: issue #1 and merge request !1
- Video: <YouTube link>

---

## Inspiration

SentinelGraph is an agentic fraud investigator for card issuers: a deterministic evidence engine over a TigerGraph of
590,000 transactions, a Gemini agent on Google ADK that explains and chooses tools but never decides, policy-as-code
with L1/L2 approval routes, and an audit trail a regulator can read. It already had tests, a Dockerfile, a Cloud Run
deploy script and a monitoring pack. What it did **not** have was anyone to run the lifecycle after a commit: review,
triage, scanning, release notes, deploys, rollback, incidents. A solo founder did all of that by hand, when there was
time, which is to say rarely.

The thesis of this entry: in a regulated domain the post-code lifecycle is the part that *should* be agentic, as long
as the agents are given the same hard rules the product gives Gemini — explain and act on the mechanics, never decide
the things humans must own.

## What it does

Push code or open an issue; agents take it from there. The only human actions left are **approve the merge request**
and **click `deploy-production`**.

| Stage | Before (GitHub Actions, by hand) | After (GitLab CI/CD + GitLab Duo) |
|---|---|---|
| plan | triage by hand | **Issue Triage flow** — labels type/severity, checks duplicates, writes acceptance criteria and an implementation sketch after reading the code |
| create | manual review | **MR Reviewer flow** — enforces the trust boundary (Gemini never decides) and golden-benchmark integrity, leaves inline findings, submits a verdict, labels the MR; foundational Duo Code Review and Duo Developer alongside |
| verify | lint + fast tests | ruff as Code Quality, fast + slow pytest (20 benchmark alerts must reproduce exactly), coverage and JUnit in the MR; **Pipeline Medic flow** reads failed job logs and pushes the fix — and refuses to touch the golden file |
| package | image built, discarded | image in the Container Registry; console, worker and intake API started from it and health-checked |
| secure | a regex for keys | SAST, secret detection, dependency scanning, container scanning; **Security Gate flow** triages findings for what matters in a bank, opens issues, blocks/clears the MR |
| release | none | keyless semantic releases: the job tags and publishes with its own `CI_JOB_TOKEN`, version from Conventional Commits; **Release Scribe flow** writes the changelog on approval, including policy version/approver and any decision change |
| configure | `deploy.ps1` from a laptop | staging deploy of three Cloud Run services via keyless OIDC (Workload Identity Federation); production behind one approval |
| monitor | Cloud Monitoring alerts (kept) | smoke tests after every deploy with automatic traffic rollback; hourly health probe |
| govern | branch protection | CODEOWNERS on decisions/policy/deploy, approval rule, DCO and license gates in CI; **Incident Responder flow** opens an Incident work item with evidence, suspected commit and rollback status |

## How we built it

- **Six custom flows** (`.gitlab/duo/flows/`), each a single `AgentComponent` (MR Reviewer adds a `OneOffComponent`
  labeler), API-only (`coding_environment: none`), with GitLab's own tools: `list_merge_request_diffs`,
  `submit_mr_review`, `get_job_logs`, `create_commit`, `list_security_findings`, `create_work_item`…
- **Triggers** on real events: work item created, merge request created / approved, pipeline passed / failed, mention.
- **`AGENTS.md`** gives every agent the two rules that matter here: *Gemini explains, the engine decides* and *never
  regenerate `tests/golden/benchmark.json`* — a changed fraud decision is a policy event for a human.
- **A 19-job pipeline** with the four scanner templates, a three-service container health check, keyless Cloud Run
  deploys (`id_tokens` → WIF → deploy service account, no JSON keys anywhere) and rollback.
- **No long-lived credential anywhere**: deploys use OIDC federation, releases use the job token, agents use
  GitLab's own service accounts. There is nothing in this project a leaked CI variable could give away.
- **Governance as code:** CODEOWNERS, DCO sign-off check, copyleft license gate, approval rule, protected `main`.

## What the agents actually did (watch issue #1 and !1)

- Issue Triage read `api/app.py` before answering and corrected the issue's premise: the endpoint already returned the
  policy once a case was done; the real gap was the provisional state. It labelled `type::feature / severity::low`.
- MR Reviewer **requested changes** on our own MR: the fallback `ans.get("policy") or POLICY.summary()` could attribute
  a finished decision to the *currently active* policy — the opposite of what an audit needs — and it named the two
  missing tests. We fixed it and asked for a re-review.
- The **second review** found three more real problems in the fix: the guard keyed on the wrong field, a test that
  patched a frozen dataclass (so it could never catch the regression it was named for), and tests that depended on
  run order. It suggested the exact remedies (`dataclasses.replace`, state-based attribution, self-contained fixtures).
- While that was being fixed the MR pipeline went red, and **Pipeline Medic** read the failed job log and pushed the
  guard fix to the branch on its own (`f8116ca1`), without touching the golden benchmark. The human commit that
  followed built on it.

- On approval, **Release Scribe** read the final diff and wrote the Keep-a-Changelog entry for !1 into
  `CHANGELOG.md` itself; on merge, the release job turned that entry into the `v1.1.0` GitLab Release and the
  post-merge pipeline rolled the image onto staging without anyone touching a cloud console.

That is the Supervised model working: agents found real defects and fixed the mechanical one themselves; a human
decided what the audit semantics should be; nothing merged on its own.

## Challenges

- Flows run in CI as a `workload` job that downloads the Duo CLI with `curl`; an `agent-config.yml` pointing at
  `python:3.11-slim` (no curl) silently killed the first runs. Lesson: the flow image must carry `curl`.
- Getting the dependency between `deploy-production` and the staging smoke test right needed explicit stages
  (`deploy-staging → verify-staging → deploy-production`), because `needs:` cannot look forward.
- A pure SPDX allowlist for licenses is whack-a-mole; a copyleft denylist is what a bank actually wants.
- Release Scribe's first changelog commit carried a skip-CI marker, which the "pipeline must succeed" merge rule
  correctly refused. The governance caught the agent; the fix went into the flow *and* into `AGENTS.md`.
- GitLab's shared runners sit on another continent from Mumbai, so a 1.5 s p95 smoke budget failed on pure
  round-trip time. The budget is now explicit (`SMOKE_P95_SECONDS`) rather than a magic number.

## Accomplishments

Nine lifecycle stages covered by a mix of CI and six agents; a review conversation between an agent and a human that
improved real code; zero secrets in the repo or the pipeline; the benchmark decisions still reproduce exactly.

## What we learned

Agents are at their best when the codebase tells them what they must not do. The `AGENTS.md` rules were the
difference between a reviewer that nitpicks and one that catches an audit bug.

## What's next

Run the Incident Responder against a real Cloud Run outage; give Pipeline Medic a `coding_environment: full` so it
can run the suite before committing; let Release Scribe open the production deploy for approval.
