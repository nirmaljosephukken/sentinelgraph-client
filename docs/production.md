# Running SentinelGraph for a client

This is the client edition. It adds sign-in, roles and an audit trail on top of the investigation engine.
The hackathon version lives in a separate folder and repository and is not affected by anything here.

## Sign-in

Every user signs in with their work account. Three modes are supported, set by `AUTH_MODE`:

| Mode | When to use it |
|------|----------------|
| `oidc` | Default for a client deployment. Google Workspace, or any OpenID Connect provider (Okta, Entra ID, Auth0). |
| `iap` | The client already puts apps behind Google Identity-Aware Proxy. The app trusts the signed IAP header. |
| `dev` | Local development and demos only. A sidebar picker lets you act as demo users. Refused on Cloud Run unless `ALLOW_DEV_AUTH=1`. |

### Set up Google sign-in (oidc)

1. Google Cloud console, project `sentinel-graph`: **APIs & Services > OAuth consent screen**.
   Choose **Internal** if everyone is in one Google Workspace, otherwise **External**, and add the app name and a support email.
2. **APIs & Services > Credentials > Create credentials > OAuth client ID**, type **Web application**.
3. Under **Authorised redirect URIs** add:
   `https://sentinelgraph-client-539369061797.asia-south1.run.app/oauth2callback`
   For local testing also add `http://localhost:8501/oauth2callback`.
4. Copy the client ID and secret into `.env` (never into chat or git):
   ```
   AUTH_CLIENT_ID=...
   AUTH_CLIENT_SECRET=...
   ADMIN_EMAILS=you@example.com
   ```
5. Deploy: `.\deploy\deploy.ps1 -Project sentinel-graph`. The script stores the client secret in Secret Manager,
   generates a cookie secret once, and sets the redirect URI for you.

For another identity provider, set `AUTH_METADATA_URL` to its discovery URL
(it ends in `/.well-known/openid-configuration`) and use that provider's client ID and secret.

### Local sign-in

`dev` mode is the default locally: `streamlit run ui/app.py`, then pick a demo user in the sidebar.
To test real sign-in locally, create `.streamlit/secrets.toml` with an `[auth]` section
(redirect_uri `http://localhost:8501/oauth2callback`, cookie_secret, client_id, client_secret,
server_metadata_url) and run with `AUTH_MODE=oidc`.

## Roles

| Role | Can do |
|------|--------|
| Viewer | Read cases, evidence and the audit trail |
| Analyst | Run investigations, submit disputes, use the agent and the scenario lab |
| Team lead | Everything above, and approve L1 actions (for example blocking a card) |
| Fraud manager | Everything above, and approve L2 actions (for example filing a report) |
| Admin | Everything above, and manage users and roles |

- Emails in `ADMIN_EMAILS` are always admins. Everyone else gets `DEFAULT_ROLE` (viewer) the first time they sign in.
- Admins change roles on the **Team & access** page. An admin cannot demote themselves, so there is always at least one admin.
- Approve and reject buttons are enforced on the server side as well as greyed out in the page.

## Audit trail

Each of these is written to an append-only log with who, which role, when and which case:
investigation run, dispute submitted, action approved, action rejected, role changed.

On Cloud Run the users and the log are stored in Firestore (`sentinelgraph-client-users`, `sentinelgraph-client-audit`).
Locally they are stored in `data/access/`. Grant the service account read-only access to the audit collection for
your compliance team, or export it to BigQuery.

## Decision policy

Every threshold, money limit and approval route lives in `config/policy.yaml`. The language model never decides
an action; the policy engine applies these numbers.

| Setting | Default | Meaning |
|---------|---------|---------|
| `thresholds.fraud_stop` | 0.85 | Act as fraud at or above this probability |
| `thresholds.legit_stop` | 0.15 | Close as legitimate at or below this probability |
| `thresholds.min_independent_evidence` | 2 | Independent lines of evidence needed before either stop |
| `thresholds.hold_authorisation` | 0.70 | Decline a pending authorisation; no block on one weak signal below it |
| `thresholds.open_case` | 0.30 | Open a case |
| `money.block_card_team_lead_max` | 2,500 | Card blocks up to this exposure need a team lead, above it a fraud manager |
| `money.report_over` | 1,000 | Confirmed fraud above this gets a suspicious activity report |
| `money.escalate_over` | 500 | Unresolved or conflicting cases above this go to an analyst |

To change the policy for a client:

1. Edit `config/policy.yaml` (or point `POLICY_FILE` at a client-specific copy).
2. Bump `policy.version` and fill in `policy.approved_by`.
3. Redeploy. The **Policies** page shows the active version, approver and fingerprint.

The file is checked when the app starts. It refuses to start if a threshold is out of range, the thresholds are
out of order (`legit_stop < open_case < hold_authorisation < fraud_stop`), an action has no route or two routes,
or a block-all-cards, report or decline action is set to run automatically.

Every case answer and every approval in the audit log records the policy version and its fingerprint, so an
auditor can tell which settings produced a decision.

## Operating modes

Set in `config/policy.yaml` under `operations.mode`, so switching is a reviewed policy change with a new version and
approver.

| Mode | What happens |
|------|--------------|
| `shadow` (default) | The agent investigates every alert and recommends actions. Nothing is acted on; the bank's current process carries on. Analysts mark each L1/L2 recommendation **Agree** or **Disagree**, and real outcomes are recorded. |
| `assist` | Auto actions are handed to the bank's systems when a case finishes, and L1/L2 actions when a team lead or fraud manager approves them. |

Start every client in shadow mode. The **Outcomes** page shows when the go-live criteria in `go_live` are met
(defaults: at least 200 cases with a real outcome, at least 90% of fraud calls really fraud, at most 2% of
legitimate calls really fraud).

### Handing actions to the bank's systems (assist mode)

SentinelGraph never touches a card itself. Set `ACTION_WEBHOOK_URL` (and `ACTION_WEBHOOK_SECRET`) in `.env`; the
deploy script passes them on, with the secret in Secret Manager. Each action is POSTed as JSON:

```json
{"event_id": "A-1001:BLOCK_CARD", "type": "action.approved", "alert_id": "A-1001", "case_id": "CASE-2026-A-1001",
 "action": "BLOCK_CARD", "route": "L1", "reason": "...", "card_id": "CT...", "affected_txn_ids": ["..."],
 "exposure": 1906.07, "verdict": "fraud", "approved_by": "lead@bank.com", "policy": "Card Fraud Policy v1.1 (...)"}
```

The receiver should check the signature and ignore repeated event ids:

```python
import hmac, hashlib
def valid(body: bytes, header: str, secret: str) -> bool:
    return hmac.compare_digest(header, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
```

Failed deliveries are retried three times, never sent twice once delivered, and written to the audit log.

## Cardholder replies

When the agent needs the cardholder (confirm a dispute, step-up authentication), a case on client data **waits for
the real answer**. It never assumes one. The Live queue shows it as *awaiting reply* with a deadline.

- An analyst records the reply on the Live queue page (not me / it was me / could not reach them, and the channel).
- The bank's contact system can send it: `POST /v1/alerts/{id}/reply` with `{"reply": "denied", "channel": "sms"}`.
- No reply within `operations.reply_wait_hours` (24 by default) continues the case as *no reply* (policy R4).
  On Cloud Run a Cloud Scheduler job checks every 15 minutes.

The case then resumes and reaches its final recommendation. The benchmark and the scenario lab keep their
simulated replies, which are labelled as assumptions.

## Outcomes

Record what really happened to each alert: on the Live queue page, as a file on the **Outcomes** page (columns
`alert_id`, `outcome`), or through `POST /v1/outcomes`. The Outcomes page compares outcomes with the agent's verdicts:
precision, missed fraud, share handed to people, calibration by probability band, results by pattern, analyst
agreement in shadow mode, and the go-live checklist.

## Input safety

The language model never decides anything: verdicts, actions, approval routes and report decisions come from the
policy engine. Input safety makes sure it also never sees personal data, and that text written by outsiders cannot
steer what it writes. Code: `agent/safety.py`.

**Personal data is masked before any text leaves for Gemini.** The Gemini client masks every outgoing request
(prompts and tool results), and the chat agent masks the analyst's messages and its tool results the same way.

| Found | Becomes |
|-------|---------|
| Card number (13 to 19 digits with a valid checksum, or any 16-digit number) | `[card ending 1111]` |
| OTP, PIN, CVV after those words | `[code]` |
| Phone number | `[phone]` |
| Email address | `[email at gmail.com]` (the domain is evidence; the mailbox is not kept) |
| Aadhaar-style 12-digit ID, PAN, IBAN, account number after "account" | `[ID number]`, `[tax ID]`, `[account number]` |

Digits in other scripts (Devanagari, Malayalam and so on) are recognised. The system's own ids (transactions,
cards, cases, and the client id formats listed in each mapping's `id_patterns`) are never masked. Dispute messages,
cardholder replies and outcome notes are stored masked; the unmasked text is not kept.

**Text written by outsiders is treated as data.** Dispute messages, alert texts, cardholder replies and fields such
as device strings and product names are checked for attempts to instruct the agent ("ignore your instructions",
"mark this as legitimate", prompt markup, hidden characters). Invisible and control characters are removed from
every loaded text field.

- A flagged dispute message or alert text still creates a case, but goes to an analyst instead of counting as a
  customer report, and the alert is marked *flagged* on the Live queue.
- Flagged values in a transaction file are loaded as plain data and listed in the upload's report.
- Every flag is written to the audit log (*input flagged*).
- The model is told, in every prompt, that such text is data and never instructions.

**Model output is checked before it is used.** A case summary, report narrative or investigator note is discarded
(and the deterministic template used instead) if it reads like instructions, contains personal data, does not state
the engine's verdict, cites an id that is not in the evidence, or claims a link between cards that the evidence does
not show. Each rejection is recorded in the case's activity.

These detectors are deliberately simple and explainable. They are one layer: the main protection is that the model
has no authority over decisions.

## Client data

Transactions and alerts arrive through the **Data intake** page or the intake API, in the client's own format.
See [data_intake.md](data_intake.md) for the mapping file, the checks, API keys and the API itself. The deploy script
adds the API as a third Cloud Run service (`sentinelgraph-client-api`).

Before loading real data, give the client its own TigerGraph graph and deploy with `-ClientGraph <name>`. That also
sets `DATA_SOURCE=client`, so evidence text never quotes figures from the benchmark dataset.

## Tests and CI

```powershell
pip install -r requirements.txt -r requirements-dev.txt
pytest -m "not slow"      # about 10 seconds: policy, safety, mapping, access, the client flow, API, webhook, lab
pytest                    # everything, about 40 seconds: adds the 20 benchmark alerts and every console page
ruff check .
```

All tests run offline (local graph, no language model, no Google Cloud). What they cover:

| File | What it proves |
|------|----------------|
| `test_policy.py` | the policy file loads, bad policies are refused, routes and decisions follow the rules (R1, R7, stop rule, replies) |
| `test_safety.py` | personal data is masked (including Indic digits), system ids are not, injection is flagged, ordinary messages are not, model output checks |
| `test_mapping.py` | client files map correctly (units, time zones, codes), bad rows are rejected with reasons, broken files are refused |
| `test_access.py` | roles, approval routes, no repeated decisions, API keys and scopes, append-only audit |
| `test_flow.py` | load, alert, wait for the cardholder, resume, timeout, flagged alert to analyst, outcomes and metrics |
| `test_api.py` | the intake API end to end with real keys |
| `test_actions.py` | signed webhook, retries, never sent twice, nothing sent in shadow mode |
| `test_scenarios.py` | the planted pattern is found on fresh synthetic banks |
| `test_benchmark.py` (slow) | the 20 benchmark alerts give exactly the decisions in `tests/golden/benchmark.json`; the answer checker passes |
| `test_ui.py` (slow) | every console page renders for an admin; viewers are kept out of admin pages |
| `test_ops.py` | structured logs, and that every alert policy counts an event the code really logs |

If a change is meant to alter a benchmark decision, regenerate `tests/golden/benchmark.json` with
`python tests/benchmark_run.py` and explain the change in the commit.

GitHub Actions (`.github/workflows/ci.yml`) runs the secret scan, lint and fast tests on every push and pull request;
the slow tests and a container check (the image builds and the console, worker and API all start and answer their
health checks) run on pushes to main, nightly and on demand. Protect the main branch so pull requests need the `test`
job to pass.

## Monitoring

Every service writes structured JSON logs (`agent/obs.py`): investigations finished and failed, every audit-log
entry (without its text), actions not delivered, flagged inputs, bad API keys, Gemini errors and rejected model
text. Personal data is never logged.

After deploying, set up monitoring once (re-running updates it):

```powershell
.\deploy\monitoring.ps1 -Project sentinel-graph -Email you@example.com
```

It creates log-based metrics, uptime checks for the console and the API, a dashboard, and these alerts, each with
a short runbook in the alert email:

| Alert | When | Severity |
|-------|------|----------|
| Actions not delivered to the bank | any failed webhook delivery (assist mode) | critical |
| Investigations failing | any investigation error | error |
| Console / intake API unreachable | health check failing for 10 minutes | error |
| Alerts waiting on the stream | oldest queued alert older than 10 minutes | error |
| Server errors | more than 5 server errors in 5 minutes | error |
| Many manipulation attempts | more than 10 flagged inputs in an hour | warning |
| Repeated bad API keys | more than 20 failed API authentications in 10 minutes | warning |
| Gemini unavailable | more than 10 Gemini errors in 15 minutes | warning |

The console's **Agent Activity** page shows the same health signals: stuck alerts, recent errors, overdue replies,
undelivered actions and investigation time.

## What is isolated from the hackathon deployment

The deploy script uses the service name as a prefix, so the client edition gets its own Cloud Run services,
Pub/Sub topic and subscription, Firestore collections and secrets. The TigerGraph graph is shared until you deploy
with `-ClientGraph`; the intake refuses to load client data into the shared demo graph.
