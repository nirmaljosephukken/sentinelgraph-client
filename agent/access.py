"""
Who may do what: roles, permissions, the user directory and the append-only audit log.

Roles (each includes the ones before it):
    viewer         read cases, the portfolio, patterns, policy and the audit log
    analyst        run investigations, send customer disputes, use the agent and the scenario lab
    team_lead      approve or reject L1 actions (e.g. block a card up to the L1 exposure limit)
    fraud_manager  approve or reject L2 actions (e.g. file a suspicious activity report)
    admin          manage users and roles, load client data, manage API keys

The user directory and the audit log live in Firestore on Google Cloud (ACCESS_BACKEND=gcp) or in local files for
development. Audit entries are only ever appended; nothing in the product edits or deletes them.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.config import ROOT, SETTINGS

ROLES = ["viewer", "analyst", "team_lead", "fraud_manager", "admin"]
ROLE_LABEL = {"viewer": "Viewer", "analyst": "Analyst", "team_lead": "Team lead (L1)",
              "fraud_manager": "Fraud manager (L2)", "admin": "Administrator"}
PERMISSIONS = {
    "view": "viewer",
    "investigate": "analyst", "intake": "analyst", "agent": "analyst", "lab": "analyst", "record": "analyst",
    "approve_L1": "team_lead",
    "approve_L2": "fraud_manager",
    "manage_users": "admin", "ingest": "admin", "manage_keys": "admin",
}
PERMISSION_LABEL = {"view": "Read cases, portfolio, policy and audit log",
                    "investigate": "Run investigations", "record": "Record cardholder replies and real outcomes", "intake": "Send customer disputes to the agent",
                    "agent": "Use the conversational agent", "lab": "Use the scenario lab",
                    "approve_L1": "Approve or reject L1 actions", "approve_L2": "Approve or reject L2 actions",
                    "manage_users": "Manage users and roles", "ingest": "Load client transactions and alerts",
                    "manage_keys": "Create and revoke API keys"}

BACKEND = os.getenv("ACCESS_BACKEND", os.getenv("LIVE_BACKEND", "local")).lower()
PREFIX = os.getenv("ACCESS_PREFIX", os.getenv("FIRESTORE_COLLECTION", "sentinelgraph").replace("-live-alerts", ""))
LOCAL_DIR = Path(os.getenv("ACCESS_DIR", ROOT / "data" / "access"))


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def can(role: str | None, permission: str) -> bool:
    need = PERMISSIONS.get(permission)
    return bool(role) and need is not None and rank(role) >= rank(need)


def route_permission(route: str) -> str | None:
    return {"L1": "approve_L1", "L2": "approve_L2"}.get(route)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def admin_emails() -> set[str]:
    import re
    return {_norm(x) for x in re.split(r"[,;]", os.getenv("ADMIN_EMAILS", "")) if x.strip()}


def default_role() -> str:
    r = os.getenv("DEFAULT_ROLE", "viewer")
    return r if r in ROLES else "viewer"


# ---------------------------------------------------------------------------------------------- stores
def _atomic(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)    # readers never see a half-written file


class _LocalStore:
    def __init__(self, d: Path = LOCAL_DIR):
        self.d = d
        self.d.mkdir(parents=True, exist_ok=True)
        self.users_p = self.d / "users.json"
        self.audit_p = self.d / "audit.jsonl"
        self._lock = threading.Lock()

    def get_user(self, email):
        users = json.loads(self.users_p.read_text(encoding="utf-8")) if self.users_p.exists() else {}
        return users.get(email)

    def put_user(self, email, doc):
        with self._lock:
            users = json.loads(self.users_p.read_text(encoding="utf-8")) if self.users_p.exists() else {}
            users[email] = {**users.get(email, {}), **doc}
            _atomic(self.users_p, json.dumps(users, indent=1))

    def list_users(self):
        users = json.loads(self.users_p.read_text(encoding="utf-8")) if self.users_p.exists() else {}
        return [{"email": k, **v} for k, v in sorted(users.items())]

    def append_audit(self, entry):
        with self._lock, self.audit_p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def _coll(self, coll):
        return self.d / f"{coll}.json"

    def get_doc(self, coll, doc_id):
        p = self._coll(coll)
        return (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}).get(doc_id)

    def put_doc(self, coll, doc_id, doc):
        with self._lock:
            p = self._coll(coll)
            docs = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            docs[doc_id] = {**docs.get(doc_id, {}), **doc}
            _atomic(p, json.dumps(docs, indent=1, default=str))

    def list_docs(self, coll):
        p = self._coll(coll)
        return [{"id": k, **v} for k, v in (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}).items()]

    def recent_audit(self, limit):
        if not self.audit_p.exists():
            return []
        lines = self.audit_p.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in reversed(lines)]


class _FirestoreStore:
    def __init__(self):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=SETTINGS.gcp_project or None)
        self.users = self.db.collection(f"{PREFIX}-users")
        self.audit = self.db.collection(f"{PREFIX}-audit")

    def get_user(self, email):
        s = self.users.document(email).get()
        return s.to_dict() if s.exists else None

    def put_user(self, email, doc):
        self.users.document(email).set(doc, merge=True)

    def list_users(self):
        return [{"email": d.id, **d.to_dict()} for d in self.users.stream()]

    def append_audit(self, entry):
        self.audit.add(entry)  # auto id: entries are never overwritten

    def get_doc(self, coll, doc_id):
        s = self.db.collection(f"{PREFIX}-{coll}").document(doc_id).get()
        return s.to_dict() if s.exists else None

    def put_doc(self, coll, doc_id, doc):
        self.db.collection(f"{PREFIX}-{coll}").document(doc_id).set(doc, merge=True)

    def list_docs(self, coll):
        return [{"id": d.id, **d.to_dict()} for d in self.db.collection(f"{PREFIX}-{coll}").stream()]

    def recent_audit(self, limit):
        q = self.audit.order_by("ts", direction=self.fs.Query.DESCENDING).limit(limit)
        return [d.to_dict() for d in q.stream()]


_store = None


def store():
    global _store
    if _store is None:
        _store = _FirestoreStore() if BACKEND == "gcp" else _LocalStore()
    return _store


# ---------------------------------------------------------------------------------------------- users
def resolve_user(email: str, name: str = "") -> dict:
    """The signed-in person's directory entry, created on first sign-in. ADMIN_EMAILS are always admins."""
    email = _norm(email)
    st = store()
    doc = st.get_user(email)
    if doc is None:
        doc = {"role": "admin" if email in admin_emails() else default_role(), "name": name, "created_at": now(),
               "created_by": "first sign-in"}
        st.put_user(email, doc)
        audit(email, doc["role"], "user_created", detail={"role": doc["role"], "how": "first sign-in"})
    elif email in admin_emails() and doc.get("role") != "admin":
        doc["role"] = "admin"
        st.put_user(email, {"role": "admin"})
    st.put_user(email, {"last_seen": now(), **({"name": name} if name and not doc.get("name") else {})})
    return {"email": email, "name": doc.get("name") or name or email, "role": doc.get("role", default_role())}


def set_role(actor: dict, email: str, role: str) -> None:
    if not can(actor.get("role"), "manage_users"):
        raise PermissionError("Only administrators can change roles")
    if role not in ROLES:
        raise ValueError(f"Unknown role {role}")
    email = _norm(email)
    if email == actor["email"] and role != "admin":
        raise PermissionError("Administrators cannot remove their own admin role")
    old = (store().get_user(email) or {}).get("role")
    store().put_user(email, {"role": role, "updated_at": now(), "updated_by": actor["email"]})
    audit(actor["email"], actor["role"], "role_changed", detail={"user": email, "from": old, "to": role})


def list_users() -> list[dict]:
    return store().list_users()


# ---------------------------------------------------------------------------------------------- audit
def audit(actor: str, role: str, action: str, case_id: str = "", detail: dict | None = None) -> dict:
    entry = {"ts": now(), "actor": _norm(actor), "role": role, "action": action, "case_id": case_id,
             "detail": json.dumps(detail or {}, default=str)}
    store().append_audit(entry)
    from agent import obs
    if action == "input_flagged":
        obs.event("input_flagged", "WARNING", source=(detail or {}).get("source"), reasons=(detail or {}).get("reasons"),
                  case_id=case_id)
    obs.event("audit", action=action, actor=_norm(actor), role=role, case_id=case_id)
    return entry


def recent_audit(limit: int = 200) -> list[dict]:
    return store().recent_audit(limit)


def decide(actor: dict, case_id: str, graph_case_id: str, action: dict, decision: str) -> dict:
    """Approve or reject a queued action. Enforces the approval route and records who decided."""
    perm = route_permission(action.get("route", ""))
    if perm is None:
        raise PermissionError(f"{action.get('action')} runs automatically and needs no approval")
    if not can(actor.get("role"), perm):
        raise PermissionError(f"{action.get('action')} needs {ROLE_LABEL[PERMISSIONS[perm]]} or above; "
                              f"you are {ROLE_LABEL.get(actor.get('role'), actor.get('role'))}")
    from agent.policy_config import POLICY
    allowed_now = ("agreed", "disagreed") if POLICY.shadow else ("approved", "rejected")
    if decision not in allowed_now:
        raise ValueError(f"in {POLICY.mode} mode the decision must be {' or '.join(allowed_now)}")
    key = f"{case_id}:{action.get('action')}"
    prev = get_doc("decisions", key)
    if prev and prev.get("decision") in allowed_now:
        raise ValueError(f"{action.get('action')} on {case_id} was already {prev['decision']} by {prev.get('actor')} "
                         f"at {str(prev.get('ts', ''))[:16].replace('T', ' ')}")
    entry = audit(actor["email"], actor["role"], f"action_{decision}", case_id,
                  {"action": action.get("action"), "route": action.get("route"), "reason": action.get("reason"),
                   "graph_case_id": graph_case_id, "policy": f"{POLICY.label} ({POLICY.fingerprint})",
                   "mode": POLICY.mode})
    put_doc("decisions", key, {"case_id": case_id, "action": action.get("action"), "route": action.get("route"),
                               "decision": decision, "actor": actor["email"], "role": actor["role"], "ts": entry["ts"],
                               "mode": POLICY.mode})
    return entry


def decision_for(case_id: str, action: str) -> dict | None:
    return get_doc("decisions", f"{case_id}:{action}")


# ---------------------------------------------------------------------------------------------- documents
def get_doc(coll: str, doc_id: str) -> dict | None:
    return store().get_doc(coll, doc_id)


def put_doc(coll: str, doc_id: str, doc: dict) -> None:
    store().put_doc(coll, doc_id, doc)


def list_docs(coll: str) -> list[dict]:
    return store().list_docs(coll)


# ---------------------------------------------------------------------------------------------- API keys
# A key looks like sgk_<key id>_<secret>. Only a SHA-256 hash of the whole key is stored; the key itself is shown
# once, when it is created. Each key is tied to one mapping (the client's file format) and a set of scopes.
KEY_SCOPES = {"transactions": "Send transactions", "alerts": "Send alerts and cardholder replies",
              "outcomes": "Send real outcomes", "read": "Read alert results"}


def _hash(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_api_key(actor: dict, name: str, mapping: str, scopes: list[str]) -> str:
    import secrets
    if not can(actor.get("role"), "manage_keys"):
        raise PermissionError("Only administrators can create API keys")
    bad = [x for x in scopes if x not in KEY_SCOPES]
    if bad or not scopes:
        raise ValueError(f"scopes must be some of {', '.join(KEY_SCOPES)}")
    if not (name or "").strip():
        raise ValueError("give the key a name, for example the client system that will use it")
    kid = secrets.token_hex(4)
    key = f"sgk_{kid}_{secrets.token_urlsafe(24)}"
    put_doc("api_keys", kid, {"name": name.strip(), "mapping": mapping, "scopes": list(scopes), "hash": _hash(key),
                              "created_at": now(), "created_by": actor["email"], "revoked": False, "last_used": ""})
    audit(actor["email"], actor["role"], "api_key_created", detail={"key_id": kid, "name": name, "mapping": mapping,
                                                                    "scopes": scopes})
    return key


def revoke_api_key(actor: dict, kid: str) -> None:
    if not can(actor.get("role"), "manage_keys"):
        raise PermissionError("Only administrators can revoke API keys")
    if not get_doc("api_keys", kid):
        raise ValueError(f"no key {kid}")
    put_doc("api_keys", kid, {"revoked": True, "revoked_at": now(), "revoked_by": actor["email"]})
    audit(actor["email"], actor["role"], "api_key_revoked", detail={"key_id": kid})


def list_api_keys() -> list[dict]:
    return sorted(({k: v for k, v in d.items() if k != "hash"} for d in list_docs("api_keys")),
                  key=lambda d: d.get("created_at", ""), reverse=True)


def check_api_key(key: str, scope: str) -> dict | None:
    """The key's record if the key is valid, not revoked and has the scope; otherwise None."""
    import hmac
    import re
    m = re.fullmatch(r"sgk_([0-9a-f]{8})_[A-Za-z0-9_-]{20,}", (key or "").strip())
    if not m:
        return None
    doc = get_doc("api_keys", m.group(1))
    if not doc or doc.get("revoked") or not hmac.compare_digest(doc.get("hash", ""), _hash(key.strip())):
        return None
    if scope not in (doc.get("scopes") or []):
        return None
    put_doc("api_keys", m.group(1), {"last_used": now()})
    return {"key_id": m.group(1), **{k: v for k, v in doc.items() if k != "hash"}}
