# Changelog

Entries under **Unreleased** are written by the Release Scribe flow when a merge request is approved and moved
into a tagged release by the release job on `main`.

## [Unreleased]

### Added
- GitLab CI/CD lifecycle: ruff as Code Quality, fast and slow pytest with JUnit/coverage in merge requests,
  policy-file check, DCO and license gates, image build with three-service health check, SAST / secret /
  dependency / container scanning, semantic releases, keyless Cloud Run deploys (staging automatic, production
  approved), smoke tests with automatic rollback, hourly health probe.
- Six GitLab Duo flows: Issue Triage, MR Reviewer, Pipeline Medic, Security Gate, Release Scribe, Incident Responder.
- `AGENTS.md` with the trust boundary and the golden-benchmark rule for agents; `CODEOWNERS` on decisions,
  policy and deployment.
- MIT license.
- `GET /v1/alerts/{id}` always reports the `policy` (name, version, approved_by, fingerprint, mode) and a new `policy_source` field (`active` or `case_record`), so auditors can tell which settings produced a decision at any point in the alert's life. (!1)

### Changed
- Alerts that are queued, investigating, awaiting the cardholder or resuming report the active policy, marked `policy_source: active`; finished alerts report only the policy recorded with their own decision (`null` if none was recorded), marked `case_record`. (!1)
- Errored alerts report no policy, so a stale first-pass answer is never presented as a decision. (!1)

## [1.0.0] — 2026-10-04

- Client edition: sign-in and roles, approvals and audit trail, policy as a reviewed settings file, data intake
  (console and API), shadow/assist modes with a signed action webhook, real cardholder replies, Outcomes page,
  input safety, 123-test suite, GitHub Actions CI, Cloud Monitoring alerts.
