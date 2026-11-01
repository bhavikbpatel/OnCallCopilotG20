"""tickets MCP server: the incident record and status notes, in our own SQLite DB.

Internal writes (create_incident, append_timeline, save_postmortem) touch only this
database, so they need no approval; the threat model justifies that. publish_status is
the one write that leaves the system: it needs an approval token for the exact text.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Optional

from eval.schemas import payload_hash
from eval.schemas.catalogue import SERVICES, normalize_severity

from tools.approval import check_token
from tools.common import err, marks, ok, redact, run_server
from tools.token_store import UsedTokens

SERVER = "tickets"
MAX_EVENT = 500
MAX_TEXT = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_name TEXT NOT NULL, service TEXT, severity TEXT,
  status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, postmortem TEXT);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, at REAL NOT NULL,
  source TEXT, event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS statuses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL, at REAL NOT NULL,
  payload_hash TEXT NOT NULL, text TEXT NOT NULL);
"""


def _iid(n: int) -> str:
    return "INC-%04d" % n


def _parse_iid(value: Any) -> Optional[int]:
    if isinstance(value, str) and value.startswith("INC-") and value[4:].isdigit():
        return int(value[4:])
    return None


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
        n = _parse_iid(incident_id)
        if n is None:
            return None
        return self.db.execute("SELECT * FROM incidents WHERE id=?", (n,)).fetchone()

    # ------------------------------------------------------------- internal writes

    def create_incident(self, alert_name: str, service: str = "", severity: str = "") -> dict:
        """WRITE (internal). Open an incident record."""
        if not isinstance(alert_name, str) or not alert_name.strip():
            return err("bad_argument", "alert_name must be a non-empty string")
        if service and service not in SERVICES:
            return err("unknown_service", "service must be one of the shop's services")
        level = normalize_severity(severity) if severity else ""
        if level not in ("", "SEV-1", "SEV-2", "SEV-3", "SEV-4", "NONE"):
            return err("bad_argument", "severity must be SEV-1..SEV-4 or NONE")
        cur = self.db.execute("INSERT INTO incidents(alert_name, service, severity, created_at) VALUES (?,?,?,?)",
                              (redact(alert_name.strip())[:200], service or None, level or None, self._clock()))
        self.db.commit()
        return ok({"incident_id": _iid(cur.lastrowid)})

    def append_timeline(self, incident_id: str, event: str, source: str = "") -> dict:
        """WRITE (internal). Add one line to an incident's timeline."""
        if self._row(incident_id) is None:
            return err("not_found", "no incident %r" % (incident_id,))
        if not isinstance(event, str) or not event.strip():
            return err("bad_argument", "event must be a non-empty string")
        self.db.execute("INSERT INTO timeline(incident_id, at, source, event) VALUES (?,?,?,?)",
                        (_parse_iid(incident_id), self._clock(), redact(str(source))[:60],
                         redact(event.strip())[:MAX_EVENT]))
        self.db.commit()
        return ok({"incident_id": incident_id})

    def save_postmortem(self, incident_id: str, text: str) -> dict:
        """WRITE (internal). Store the postmortem draft; a person reviews it before memory keeps lessons."""
        if self._row(incident_id) is None:
            return err("not_found", "no incident %r" % (incident_id,))
        if not isinstance(text, str) or not text.strip():
            return err("bad_argument", "text must be a non-empty string")
        self.db.execute("UPDATE incidents SET postmortem=? WHERE id=?", (redact(text)[:MAX_TEXT * 4], _parse_iid(incident_id)))
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
        d = found["data"]
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
                              (_parse_iid(incident_id), self._clock(), digest, text))
        self.db.commit()
        return ok({"status_id": "ST-%04d" % cur.lastrowid, "payload_hash": digest, "published": True})


def build_server(tickets: Optional[Tickets] = None):
    """Build the FastMCP server. Run with:  python -m tools.tickets.server"""
    import os
    from fastmcp import FastMCP

    t = tickets or Tickets(os.environ.get("INCIDENT_DB", "incidents.db"))
    mcp = FastMCP("tickets")

    @mcp.tool(**marks(SERVER, "create_incident"))
    def create_incident(alert_name: str, service: str = "", severity: str = "") -> dict:
        """WRITE (internal). Open an incident record; returns incident_id."""
        return t.create_incident(alert_name, service, severity)

    @mcp.tool(**marks(SERVER, "append_timeline"))
    def append_timeline(incident_id: str, event: str, source: str = "") -> dict:
        """WRITE (internal). Add one line to the incident timeline."""
        return t.append_timeline(incident_id, event, source)

    @mcp.tool(**marks(SERVER, "get_incident"))
    def get_incident(incident_id: str) -> dict:
        """READ. The incident record with its timeline."""
        return t.get_incident(incident_id)

    @mcp.tool(**marks(SERVER, "draft_status"))
    def draft_status(incident_id: str) -> dict:
        """READ. Draft a status note from the timeline; returns the exact payload and hash to approve."""
        return t.draft_status(incident_id)

    @mcp.tool(**marks(SERVER, "save_postmortem"))
    def save_postmortem(incident_id: str, text: str) -> dict:
        """WRITE (internal). Store the postmortem draft."""
        return t.save_postmortem(incident_id, text)

    @mcp.tool(**marks(SERVER, "publish_status"))
    def publish_status(incident_id: str, text: str, approval_token: str) -> dict:
        """WRITE (external, needs approval of this exact text)."""
        return t.publish_status(incident_id, text, approval_token)

    return mcp


if __name__ == "__main__":
    run_server(build_server(), "tickets")
