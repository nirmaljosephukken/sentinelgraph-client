# Demo video — shot list (target 2:40, hard limit 3:00)

Record at 1920×1080, browser zoom 110 %, dark theme off (GitLab's light theme reads better compressed).
Open every tab beforehand; the video is tab-switching plus voice-over. No slides. Speak slowly — ~110 words/min.

| # | Time | On screen | Say (voice-over) |
|---|------|-----------|------------------|
| 1 | 0:00–0:15 | README top of https://gitlab.com/nekku-solo/sentinelgraph-client, scroll slowly to the **Life After Code** table | "SentinelGraph is a fraud investigator for card issuers. A Gemini agent explains, a deterministic engine decides. Until this week, everything after a commit — review, triage, scans, releases, deploys, incidents — was one person doing it by hand. This entry hands that lifecycle to GitLab Duo agents." |
| 2 | 0:15–0:35 | `.gitlab/duo/flows/` folder, open `mr-reviewer.yaml`, scroll to the trust-boundary rules, then `AGENTS.md` | "Six custom flows. Each one is given the two rules that matter in a bank: Gemini never decides, and nobody — human or agent — regenerates the golden benchmark. A changed fraud decision is a policy event, not a code change." |
| 3 | 0:35–0:50 | **Issue #1** — the triage note from `ai-sentinelgraph-issue-triage` with its labels | "Open an issue and the triage agent reads the code before answering. Here it corrected my premise — the endpoint already returned the policy once a case was done; the real gap was the provisional state — and labelled it." |
| 4 | 0:50–1:25 | **MR !1**, scroll the review: first "requested changes" → my fix commit → the **second** review (frozen dataclass, order-dependent tests) → Pipeline Medic's own commit `f8116ca1` → my commit `ae66093b` | "This is the part I want you to watch. I opened the MR and the reviewer agent requested changes: my fallback could attribute an old decision to today's policy — the opposite of what an audit needs. I fixed it. It reviewed again and found three more things, including a test that could never catch the bug it was named for. While I was fixing that, the pipeline went red, and the Pipeline Medic agent pushed the guard fix on its own. Nothing merged. A human still approves." |
| 5 | 1:25–1:45 | **Automate → Sessions** page, open one MR-reviewer session, scroll the tool calls (`list_merge_request_diffs`, `read_file`, `submit_mr_review`) | "Every agent run is a session you can audit: which files it read, which tools it called, what it posted. The same standard we hold the fraud agent to." |
| 6 | 1:45–2:05 | **Pipeline graph** on main (green): verify → package → secure → release → deploy-staging → verify-staging → deploy-production (manual) → monitor | "The pipeline: lint, fast and slow suites — twenty benchmark alerts must reproduce exactly — the image built and all three services health-checked from it, four scanners, a release, a keyless Cloud Run deploy to staging with smoke tests and automatic rollback. Production is one manual click behind an approval." |
| 7 | 2:05–2:25 | `.gitlab-ci.yml` at the `.gcloud` block (`id_tokens`, WIF), then **Settings → CI/CD → Variables** showing only `GCP_*` names (no values) | "No JSON keys anywhere. The job's OIDC token is exchanged for a deploy identity through Workload Identity Federation, restricted to this project's protected branch." |
| 8 | 2:25–2:40 | README before/after table, end on the project URL | "Nine lifecycle stages, six agents, a review conversation that made real code better, and the benchmark decisions unchanged. Supervised autonomy: the agents do the work; the humans own the decisions." |

## Before recording — checklist

- [ ] MR !1 pipeline green and the third review posted (if it still requests changes, show that — it is a better story than a rubber stamp).
- [ ] `main` pipeline green; if the GCP variables are set, one staging deploy visible under **Deployments → Environments**.
- [ ] Close other tabs; hide bookmarks bar; sign out of anything personal in the same window.
- [ ] Do a dry run with a timer — shot 4 always runs long.

## Upload

YouTube, **Unlisted**, title "SentinelGraph — Life After Code (GitLab Transcend Hackathon)". Paste the link into the
Devpost form and into `docs/devpost.md`.
