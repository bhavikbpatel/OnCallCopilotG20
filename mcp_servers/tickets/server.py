"""tickets MCP server: the incident record and status notes, in our own SQLite DB.

Internal writes (create_incident, append_timeline, save_postmortem) touch only this
database, so they need no approval; the threat model justifies that. publish_status is
the one write that leaves the system: it needs an approval token for the exact text.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from eval.schemas import payload_hash
from eval.schemas.catalogue import SERVICES, SEVERITY_LEVELS, normalize_severity

from mcp_servers.common.approval import check_token
from mcp_servers.common import err, marks, ok, redact, run_server
from mcp_servers.common.models import typed_output
from mcp_servers.common.token_store import UsedTokens

SERVER = "tickets"
MAX_EVENT = 500
MAX_TEXT = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT UNIQUE, alert_name TEXT NOT NULL, service TEXT,
  severity TEXT, status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, postmortem TEXT);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, at REAL NOT NULL,
  source TEXT, event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS statuses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, at REAL NOT NULL,
  payload_hash TEXT NOT NULL, text TEXT NOT NULL);
"""


def _iid(n: int) -> str:
    return "INC-%04d" % n


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def _epoch(value: Any) -> Optional[float]:
    """`at` from the graph is ISO-8601 (or epoch seconds). None means it could not be read."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _alert_fields(alert: Any) -> tuple:
    """(alert_name, service) from the graph's Alert object (copilot/state.py: `alert_name`, `service`).
    The Prometheus-style names are accepted too. Returns "" for anything missing; the caller decides."""
    if not isinstance(alert, dict):
        return "", ""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    name = alert.get("alert_name") or alert.get("alertname") or alert.get("name") or labels.get("alertname") or ""
    service = alert.get("service") or labels.get("service_name") or labels.get("service") or ""
    return str(name), str(service)


class Tickets:
    def __init__(self, db_path: str = ":memory:", secret: Optional[str] = None,
                 clock: Callable[[], float] = time.time, used_tokens: Optional[UsedTokens] = None):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._secret = secret
        self._clock = clock
        self._used = used_tokens or UsedTokens()

    def _row(self, incident_id: str):
        if not isinstance(incident_id, str):
            return None
        return self.db.execute("SELECT * FROM incidents WHERE public_id=?", (incident_id,)).fetchone()

    # ------------------------------------------------------------- internal writes

    def create_incident(self, incident_id: str = "", alert: Optional[dict] = None, severity: str = "",
                        alert_name: str = "", service: str = "") -> dict:
        """WRITE (internal). Open an incident record. The graph passes its own incident_id and its Alert
        (`alert_name`, `service`); without an incident_id one is generated.

        Refused, never guessed: an alert with no name (missing_alert_name), a malformed incident_id or severity.
        An alert whose service is not one of the shop's services still opens the incident (alert data is
        untrusted and an outage must be handled): the service is left empty and `warnings` says so.
        Calling again with an existing incident_id changes nothing and says so in `warnings`."""
        if incident_id and not (isinstance(incident_id, str) and _ID.fullmatch(incident_id)):
            return err("bad_argument", "incident_id must be 1-64 letters, digits or . _ : -")
        if incident_id and self._row(incident_id) is not None:
            return ok({"incident_id": incident_id, "warnings": ["incident_already_exists"]})
        from_alert, alert_service = _alert_fields(alert)
        name = (alert_name or from_alert).strip()
        if not name:
            return err("missing_alert_name", "the alert has no alert_name")
        warnings = []
        if service:
            if service not in SERVICES:      # an explicit service argument is a programming error
                return err("unknown_service", "service must be one of the shop's services")
        else:
            service = alert_service
            if service and service not in SERVICES:
                warnings.append("service_not_recognised:%s" % redact(service)[:40])
                service = ""
        level = normalize_severity(severity) if severity else ""
        if level and level not in SEVERITY_LEVELS:
            return err("bad_argument", "severity must be one of %s (got %r)" % (", ".join(SEVERITY_LEVELS), str(severity)[:20]))
        cur = self.db.execute("INSERT INTO incidents(alert_name, service, severity, created_at) VALUES (?,?,?,?)",
                              (redact(name)[:200], service or None, level or None, self._clock()))
        public = incident_id or _iid(cur.lastrowid)
        self.db.execute("UPDATE incidents SET public_id=? WHERE id=?", (public, cur.lastrowid))
        self.db.commit()
        return ok({"incident_id": public, "warnings": warnings})

    def append_timeline(self, incident_id: str, text: str = "", at: Any = None, event: str = "",
                        source: str = "") -> dict:
        """WRITE (internal). Add one line to an incident's timeline (`text`, or `event`; `at` is ISO-8601 or epoch
        seconds and defaults to now when not given; an `at` that cannot be read is refused)."""
        row = self._row(incident_id)
        if row is None:
            return err("not_found", "no incident %r" % (incident_id,))
        line = text or event
        if not isinstance(line, str) or not line.strip():
            return err("bad_argument", "text must be a non-empty string")
        when = self._clock() if at in (None, "") else _epoch(at)
        if when is None:
            return err("bad_argument", "at must be ISO-8601 or epoch seconds, got %r" % str(at)[:40])
        self.db.execute("INSERT INTO timeline(incident_id, at, source, event) VALUES (?,?,?,?)",
                        (row["id"], when, redact(str(source or ""))[:60], redact(line.strip())[:MAX_EVENT]))
        self.db.commit()
        return ok({"incident_id": incident_id})

    def save_postmortem(self, incident_id: str, postmortem: Any = None, text: str = "") -> dict:
        """WRITE (internal). Store the postmortem draft (the graph's Postmortem object, or plain text);
        a person reviews it before memory keeps lessons."""
        row = self._row(incident_id)
        if row is None:
            return err("not_found", "no incident %r" % (incident_id,))
        body = json.dumps(postmortem, sort_keys=True, default=str) if isinstance(postmortem, (dict, list)) else (postmortem or text)
        if not isinstance(body, str) or not body.strip():
            return err("bad_argument", "postmortem must be a non-empty object or text")
        self.db.execute("UPDATE incidents SET postmortem=? WHERE id=?", (redact(body)[:MAX_TEXT * 4], row["id"]))
        self.db.commit()
        return ok({"incident_id": incident_id})

    # ------------------------------------------------------------- reads

    def get_incident(self, incident_id: str) -> dict:
        """READ. The incident record with its timeline."""
        row = self._row(incident_id)
        if row is None:
            return err("not_found", "no incident %r" % (incident_id,))
        events = self.db.execute("SELECT at, source, event FROM timeline WHERE incident_id=? ORDER BY id",
                                 (row["id"],)).fetchall()
        return ok({"incident_id": incident_id, "alert_name": row["alert_name"], "service": row["service"],
                   "severity": row["severity"], "status": row["status"],
                   "timeline": [{"at": e["at"], "source": e["source"], "event": e["event"]} for e in events],
                   "postmortem": row["postmortem"]})

    def draft_status(self, incident_id: str) -> dict:
        """READ. A status note drafted from the incident's own timeline, and the exact payload to approve."""
        found = self.get_incident(incident_id)
        if not found["ok"]:
            return found
        d = found["result"]
        lines = ["Incident %s: %s%s%s." % (incident_id, d["alert_name"],
                                            " on %s" % d["service"] if d["service"] else "",
                                            " (%s)" % d["severity"] if d["severity"] else "")]
        for e in d["timeline"]:
            stamp = time.strftime("%H:%M", time.gmtime(e["at"]))
            lines.append("%s UTC %s%s" % (stamp, "%s: " % e["source"] if e["source"] else "", e["event"]))
        text = "\n".join(lines)[:MAX_TEXT]
        payload = {"incident_id": incident_id, "text": text}
        return ok({"text": text, "payload": payload, "payload_hash": payload_hash("publish_status", payload)})

    # ------------------------------------------------------------- external write

    def publish_status(self, incident_id: str, text: str, approval_token: str = "") -> dict:
        """WRITE (external, gated). Publish exactly this text; the token must be for this exact payload."""
        if self._row(incident_id) is None:
            return err("not_found", "no incident %r" % (incident_id,))
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return err("bad_argument", "text must be 1-%d characters" % MAX_TEXT)
        if redact(text) != text:
            return err("secret_in_text", "the status text contains a secret or personal data; remove it")
        digest = payload_hash("publish_status", {"incident_id": incident_id, "text": text})
        code = check_token(digest, approval_token, self._secret, self._clock())
        if code:
            return err(code, "the approval token does not authorise exactly this status text")
        if not self._used.claim(approval_token):
            return err("approval_already_used", "this approval token was already used")
        cur = self.db.execute("INSERT INTO statuses(incident_id, at, payload_hash, text) VALUES (?,?,?,?)",
                              (self._row(incident_id)["id"], self._clock(), digest, text))
        self.db.commit()
        return ok({"status_id": "ST-%04d" % cur.lastrowid, "payload_hash": digest, "published": True})


def build_server(tickets: Optional[Tickets] = None):
    """Build the FastMCP server. Run with:  python -m mcp_servers.tickets.server"""
    import os
    from fastmcp import FastMCP

    t = tickets or Tickets(os.environ.get("INCIDENT_DB", "incidents.db"))
    mcp = FastMCP("tickets")

    @mcp.tool(**marks(SERVER, "create_incident"))
    @typed_output(SERVER, "create_incident")
    def create_incident(incident_id: str = "", alert: Optional[dict] = None, severity: str = "",
                        alert_name: str = "", service: str = "") -> dict:
        """WRITE (internal). Open an incident record; returns incident_id."""
        return t.create_incident(incident_id, alert, severity, alert_name, service)

    @mcp.tool(**marks(SERVER, "append_timeline"))
    @typed_output(SERVER, "append_timeline")
    def append_timeline(incident_id: str, text: str = "", at: str = "", event: str = "", source: str = "") -> dict:
        """WRITE (internal). Add one line to the incident timeline."""
        return t.append_timeline(incident_id, text, at, event, source)

    @mcp.tool(**marks(SERVER, "get_incident"))
    @typed_output(SERVER, "get_incident")
    def get_incident(incident_id: str) -> dict:
        """READ. The incident record with its timeline."""
        return t.get_incident(incident_id)

    @mcp.tool(**marks(SERVER, "draft_status"))
    @typed_output(SERVER, "draft_status")
    def draft_status(incident_id: str) -> dict:
        """READ. Draft a status note from the timeline; returns the exact payload and hash to approve."""
        return t.draft_status(incident_id)

    @mcp.tool(**marks(SERVER, "save_postmortem"))
    @typed_output(SERVER, "save_postmortem")
    def save_postmortem(incident_id: str, postmortem: Optional[dict] = None, text: str = "") -> dict:
        """WRITE (internal). Store the postmortem draft."""
        return t.save_postmortem(incident_id, postmortem, text)

    @mcp.tool(**marks(SERVER, "publish_status"))
    @typed_output(SERVER, "publish_status")
    def publish_status(incident_id: str, text: str, approval_token: str) -> dict:
        """WRITE (external, needs approval of this exact text)."""
        return t.publish_status(incident_id, text, approval_token)

    return mcp


if __name__ == "__main__":
    run_server(build_server(), "tickets")
