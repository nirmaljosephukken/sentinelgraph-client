# Sending your data to SentinelGraph

SentinelGraph investigates alerts against your own card transactions. You send two things, in your own format:

1. **Transactions**, as often as you like (a nightly export, hourly files, or a stream of small batches).
2. **Alerts**, each pointing at one transaction you have already sent: a rule or model hit, a customer dispute, or
   an analyst request. Each alert is investigated within seconds and the result can be read back.

Your column names, date formats and codes stay as they are. A **mapping file** translates them once.

## What SentinelGraph needs

Only four transaction fields are required. Everything else makes investigations better when it is there.

| Field | Required | What it enables |
|-------|----------|-----------------|
| Transaction id | yes | must be unique; sending the same id again replaces that transaction |
| Transaction time | yes | velocity, sequences, timelines |
| Amount | yes | exposure, card testing, structuring, recurring charges |
| Card id or token | yes | card history; a token is fine, SentinelGraph never needs the card number |
| Customer id | | prior cases, the block-all-cards rule |
| Channel (online or in person) | | online versus in-person rules |
| Product or merchant category | | recurring-charge recognition |
| Bank risk score (0 to 1) | | shown as context, never treated as a verdict |
| Billing region | | out-of-region use |
| Email domain | | links between cards (only the domain is kept, never the mailbox) |
| Device fingerprint | | device sharing and device rings: the strongest network evidence |
| Device seen before | | new-device evidence |
| Proxy or VPN flag | | anonymous-connection evidence |

Do not send card numbers (PAN), CVV, full names or full email addresses: the intake drops the mailbox part of an
email and stores only the domain, and nothing else needs personal data. If personal data does slip into a text field
(an alert text, a reply), it is masked before the language model sees it; see "Input safety" in production.md.

Alerts need an alert id and the id of the flagged transaction. A type (rule or model, dispute, analyst request),
a text and a time are optional.

The full list is at `GET /v1/schema`, and in `agent/ingest/schema.py`.

## The mapping file

One YAML file per format, in `config/mappings/`. See `example_bank.yaml` for a bank that exports Indian dates,
amounts in paise, scores as percentages and channel codes:

```yaml
timezone: Asia/Kolkata
transactions:
  id: TXN_REF
  ts: {column: TXN_DATE_TIME, format: "%d/%m/%Y %H:%M:%S"}
  amount: {column: AMOUNT_PAISE, scale: 0.01}
  card_id: CARD_TOKEN
  channel: {column: CHANNEL, values: {ECOM: online, POS: in_person, "*": in_person}}
  p_email: {column: CUSTOMER_EMAIL, email_domain: true, lower: true}
  device_status: {column: DEVICE_SEEN_BEFORE, values: {Y: Found, N: New}}
alerts:
  case_id: ALERT_ID
  flagged_txn_id: TXN_REF
  trigger_type: {column: ALERT_TYPE, values: {RULE: risk_score, DISPUTE: customer_report, "*": analyst_request}}
```

Rule options: `column`, `join` (several columns, with `sep`), `value` (a constant), `format` (time format, or
`epoch_s`, `epoch_ms`, `iso`), `scale`, `values` (a code table; `"*"` is the fallback), `email_domain`,
`prefix` (first characters), `lower`, `upper`, `default`. `id_patterns` lists the shape of your ids so the
language-model guardrail can spot an id that does not come from the evidence. `max_rejected_share` (default 5%)
refuses a whole file when too many rows fail, so a broken export never half-loads.

## Checks on every file

Before anything reaches the graph, every row is checked: required fields present, numbers and times parse, no time
more than a day in the future, no duplicate id within the file, scores between 0 and 1, codes known. Rows that
fail are listed with the row number, the field, the value and the reason. Fields that cannot be filled are
reported as the evidence they would have enabled, so you can see what a richer export would add.

## Two ways to send data

**Console.** An administrator opens **Data intake**, picks the mapping, uploads a file, reads the check report and
a preview, then loads it. Alerts are uploaded on the same page and followed on the **Live queue** page.

**API.** An administrator creates an API key on **Team & access**: name it after the system that will use it, pick
the mapping, choose scopes (send transactions, send alerts, read results). The key is shown once.

```bash
API=https://sentinelgraph-client-api-<project-number>.<region>.run.app
KEY=sgk_...

# check a file without loading it
curl -s -X POST "$API/v1/transactions?dry_run=true" -H "Authorization: Bearer $KEY" \
     -H "Content-Type: text/csv" --data-binary @transactions.csv

# load it
curl -s -X POST "$API/v1/transactions" -H "Authorization: Bearer $KEY" \
     -H "Content-Type: text/csv" --data-binary @transactions.csv

# send alerts (CSV, or JSON as below)
curl -s -X POST "$API/v1/alerts" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"records": [{"ALERT_ID": "A-1001", "TXN_REF": "TXN400005260", "ALERT_TYPE": "RULE"}]}'

# read the result
curl -s "$API/v1/alerts/A-1001" -H "Authorization: Bearer $KEY"

# record the cardholder's answer when a case is waiting for it (status "awaiting_reply")
curl -s -X POST "$API/v1/alerts/A-1001/reply" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"reply": "denied", "channel": "sms", "text": "not me, card is with me"}'

# record what really happened (chargebacks, confirmed cases, false alarms)
curl -s -X POST "$API/v1/outcomes" -H "Authorization: Bearer $KEY" -H "Content-Type: text/csv" \
     --data-binary $'alert_id,outcome\nA-1001,fraud\nA-1002,legitimate\n'
```

Key scopes: send transactions, send alerts and cardholder replies, send outcomes, read results.

Bodies can be CSV, TSV, JSON lines (`application/x-ndjson`), a JSON array or `{"records": [...]}`, up to 25 MB and
200,000 rows per call. Interactive documentation is at `$API/docs`.

| Status | Meaning |
|--------|---------|
| 200 | transactions loaded (or checked, for a dry run) |
| 202 | alerts queued (or a reply accepted); poll `GET /v1/alerts/{id}` until `status` is `done` or `awaiting_reply` |
| 409 | a reply for an alert that is not waiting for one |
| 401 / 403 | no key, or the key is invalid, revoked or lacks the scope |
| 413 | body too large; split it |
| 422 | the file was refused: the response lists every rejected row and why; nothing was loaded |

A finished alert returns the verdict, fraud probability, pattern, exposure, the recommended actions with their
approval route, whether a suspicious activity report is due, the case id and the policy version that decided it.
Actions that need a team lead or fraud manager wait for approval in the console.

Every upload, accepted or refused, is kept with its report under **Data intake > Upload history** and written to
the audit log with the key or person that sent it.

## Where the data goes

On Cloud Run, transactions are written to the client's own TigerGraph graph. The intake refuses to write to the
demo graph. Set the client graph up once:

```powershell
# in .env: TG_GRAPH=AcmeBankGraph
python graph/setup_graph.py --empty          # schema, policy knowledge and queries, no demo data
.\deploy\deploy.ps1 -Project <gcp-project> -ClientGraph AcmeBankGraph
```

Locally (no TigerGraph), uploads are kept in `data/client/` and loaded into the in-memory graph.
`DATA_SOURCE=client` shows only client data, `demo` only the benchmark dataset, and `both` (the default for
development) shows both.
