"""
Operational events as structured logs.

On Cloud Run, one JSON object per line on stdout becomes a structured Cloud Logging entry: `severity` sets the level,
`message` the summary, everything else lands in jsonPayload. deploy/monitoring.ps1 turns these events into
log-based metrics, alert policies and a dashboard. Locally the same lines go to the console.

Events (jsonPayload.event):
    service_started        component, policy, fingerprint, mode, data_source
    investigation_done     alert_id, seconds, verdict, status, source
    investigation_error    alert_id, error                              (ERROR)
    audit                  action, actor, role, case_id                 (every audit-log entry, without its detail)
    action_send_failed     event_id, detail                             (ERROR)
    input_flagged          source, reasons                              (WARNING)
    api_auth_failed        reason, path                                 (WARNING)
    llm_error              provider, error                              (WARNING; the case falls back to templates)
    llm_output_rejected    field, reason                                (WARNING)
Never log personal data: callers pass ids, counts and reasons, not message text.
"""
from __future__ import annotations

import json
import os
import sys
import time

SERVICE = os.getenv("K_SERVICE") or os.getenv("ACCESS_PREFIX") or "sentinelgraph-local"
_started: set[str] = set()


def event(name: str, severity: str = "INFO", message: str = "", **fields) -> None:
    rec = {"severity": severity, "message": message or name.replace("_", " "), "event": name, "service": SERVICE,
           "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    try:
        sys.stdout.write(json.dumps(rec, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001  logging must never break the work
        pass


def started(component: str) -> None:
    """Once per process: which policy and data source this instance runs with."""
    if component in _started:
        return
    _started.add(component)
    try:
        from agent.policy_config import POLICY
        event("service_started", component=component, policy=POLICY.label, fingerprint=POLICY.fingerprint,
              mode=POLICY.mode, data_source=os.getenv("DATA_SOURCE", "both"))
    except Exception as ex:  # noqa: BLE001
        event("service_started", "WARNING", component=component, error=str(ex)[:200])
