#!/usr/bin/env python3
"""
servicenow_sync.py
Fetches ALL records from ServiceNow (incidents, change requests, and more)
irrespective of state/status, and writes them as structured Markdown files
optimised for FAISS vector search and GitHub Copilot agent retrieval.

Output layout
─────────────
knowledge/
  incident/          ← one .md per incident
  change_request/    ← one .md per change order
  problem/
  kb_knowledge/
  sc_req_item/
  sc_task/
  _meta/
    manifest.json    ← record counts, sync timestamp, schema per table
"""

import os
import re
import json
import time
import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ══════════════════════════════════════════════════════════════
# Environment / Config
# ══════════════════════════════════════════════════════════════

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE", "")
SNOW_USER     = os.getenv("SNOW_USER", "")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD", "")

missing = [v for v, k in [("SNOW_INSTANCE", SNOW_INSTANCE),
                           ("SNOW_USER",     SNOW_USER),
                           ("SNOW_PASSWORD", SNOW_PASSWORD)] if not k]
if missing:
    raise EnvironmentError(f"Missing environment variables: {missing}")

BASE_URL = (
    SNOW_INSTANCE.rstrip("/")
    if SNOW_INSTANCE.startswith("https://")
    else f"https://{SNOW_INSTANCE}.service-now.com"
)

KNOWLEDGE_DIR = Path("knowledge")
META_DIR      = KNOWLEDGE_DIR / "_meta"

# ══════════════════════════════════════════════════════════════
# Table Definitions
# query=""  →  NO filter  →  ALL records regardless of state
# ══════════════════════════════════════════════════════════════

TABLES = {
    "incident": {
        "query":     "",        # ALL incidents — open, closed, resolved, cancelled
        "page_size": 1000,
        # Fields surfaced prominently in the Markdown header block.
        # All other fields are still written under "All Fields".
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority", "severity", "urgency",
            "category", "subcategory",
            "assignment_group", "assigned_to",
            "caller_id", "opened_by", "opened_at",
            "resolved_at", "closed_at", "close_notes",
            "cmdb_ci", "impact", "active",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "change_request": {
        "query":     "",        # ALL change orders — all types and states
        "page_size": 1000,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "type", "phase", "risk", "impact",
            "priority", "category",
            "assignment_group", "assigned_to",
            "requested_by", "start_date", "end_date",
            "opened_at", "closed_at",
            "cmdb_ci", "justification", "implementation_plan",
            "backout_plan", "test_plan",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "problem": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority", "impact",
            "assignment_group", "assigned_to",
            "opened_at", "resolved_at",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "kb_knowledge": {
        "query":     "workflow_state=published",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "text",
            "category", "kb_category", "author",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "sc_req_item": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "stage", "priority",
            "cat_item", "request", "assigned_to",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
    "sc_task": {
        "query":     "",
        "page_size": 500,
        "headline_fields": [
            "number", "short_description", "description",
            "state", "priority",
            "assignment_group", "assigned_to",
            "sys_created_on", "sys_updated_on", "sys_id",
        ],
    },
}

# ══════════════════════════════════════════════════════════════
# HTTP helpers
# ══════════════════════════════════════════════════════════════

AUTH    = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)
HEADERS = {"Accept": "application/json"}

MAX_RETRIES   = 4
RETRY_BACKOFF = 2   # seconds; doubles each retry


def _fetch_page(table: str, query: str, limit: int, offset: int) -> list:
    url    = f"{BASE_URL}/api/now/table/{table}"
    params = {
        "sysparm_limit":                  limit,
        "sysparm_offset":                 offset,
        "sysparm_display_value":          "all",   # {value, display_value} per field
        "sysparm_exclude_reference_link": "true",
    }
    if query:
        params["sysparm_query"] = query

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, auth=AUTH, headers=HEADERS,
                             params=params, timeout=120)
            r.raise_for_status()
            return r.json().get("result", [])
        except requests.exceptions.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF ** attempt
            print(f"    attempt {attempt} failed ({exc}) — retry in {wait}s")
            time.sleep(wait)
    return []


def fetch_all(table: str, config: dict) -> list:
    records, offset = [], 0
    ps = config["page_size"]
    q  = config.get("query", "")
    print(f"\n▶  {table}  (filter={q!r or 'NONE — fetching ALL records'})")
    while True:
        print(f"   offset={offset} …", end=" ", flush=True)
        page = _fetch_page(table, q, ps, offset)
        print(f"{len(page)} records")
        records.extend(page)
        if len(page) < ps:
            break
        offset += ps
    print(f"   ✓ total {len(records)} records")
    return records


# ══════════════════════════════════════════════════════════════
# Value extraction helpers
# When sysparm_display_value=all, each field is a dict:
#   {"value": "raw_code", "display_value": "Human Label"}
# ══════════════════════════════════════════════════════════════

def _val(field_data) -> str:
    """Human-readable display value, falling back to raw."""
    if isinstance(field_data, dict):
        dv = field_data.get("display_value", "")
        rv = field_data.get("value", "")
        return dv if dv else rv
    return str(field_data) if field_data is not None else ""


def _raw(field_data) -> str:
    """Raw API value only."""
    if isinstance(field_data, dict):
        return str(field_data.get("value", ""))
    return str(field_data) if field_data is not None else ""


def _record_id(item: dict) -> str:
    for key in ("number", "name", "sys_id"):
        v = _val(item.get(key, ""))
        if v:
            return re.sub(r'[^\w\-]', '_', v)
    return "unknown"


# ══════════════════════════════════════════════════════════════
# Markdown renderer
#
# Structure is tuned for FAISS + GitHub Copilot agent retrieval:
#
#  1. YAML front-matter  →  structured metadata for pre-filtering
#                           before semantic search (table, state,
#                           priority, dates, IDs)
#
#  2. ## Summary block   →  dense human-readable headline fields.
#                           Most agent queries will match here.
#
#  3. ## Description / Resolution / Plan blocks
#                        →  natural chunk boundaries; long-form text
#                           gets its own embedding chunk
#
#  4. ## All Fields table →  complete field coverage; used for
#                            precise field-level lookups
#
#  5. ## Raw JSON         →  exact-match safety net; last so it
#                            doesn't dominate the embedding
# ══════════════════════════════════════════════════════════════

def render_markdown(table: str, item: dict, headline_fields: list) -> str:
    rid        = _record_id(item)
    short_desc = _val(item.get("short_description", "")) or "(no description)"

    lines = []

    # ── YAML front-matter ──────────────────────────────────────
    fm = {
        "record_id":  rid,
        "table":      table,
        "sys_id":     _raw(item.get("sys_id", "")),
        "state":      _val(item.get("state", "")),
        "priority":   _val(item.get("priority", "")),
        "category":   _val(item.get("category", "")),
        "opened_at":  _val(item.get("opened_at", "")),
        "updated_at": _val(item.get("sys_updated_on", "")),
    }
    if table == "change_request":
        fm["change_type"] = _val(item.get("type", ""))
        fm["phase"]       = _val(item.get("phase", ""))
        fm["risk"]        = _val(item.get("risk", ""))
    if table == "incident":
        fm["severity"] = _val(item.get("severity", ""))
        fm["urgency"]  = _val(item.get("urgency", ""))
        fm["impact"]   = _val(item.get("impact", ""))

    lines.append("---")
    for k, v in fm.items():
        lines.append(f'{k}: "{str(v).replace(chr(34), chr(39))}"')
    lines.append("---")
    lines.append("")

    # ── Title ──────────────────────────────────────────────────
    lines.append(f"# {table.upper()} {rid}")
    lines.append(f"**{short_desc}**")
    lines.append("")

    # ── Summary (dense; agent hits this first) ─────────────────
    lines.append("## Summary")
    lines.append("")
    for f in headline_fields:
        v = _val(item.get(f, ""))
        if v and f not in ("description", "text",
                            "close_notes", "justification",
                            "implementation_plan", "backout_plan",
                            "test_plan"):
            label = f.replace("_", " ").title()
            lines.append(f"- **{label}**: {v}")
    lines.append("")

    # ── Description ────────────────────────────────────────────
    desc = _val(item.get("description", "")) or _val(item.get("text", ""))
    if desc and desc.strip():
        lines.append("## Description")
        lines.append("")
        lines.append(desc.strip())
        lines.append("")

    # ── Resolution / close notes ───────────────────────────────
    close_notes = _val(item.get("close_notes", ""))
    if close_notes and close_notes.strip():
        lines.append("## Resolution Notes")
        lines.append("")
        lines.append(close_notes.strip())
        lines.append("")

    # ── Change-specific plan sections ─────────────────────────
    for field, label in [
        ("justification",       "Justification"),
        ("implementation_plan", "Implementation Plan"),
        ("backout_plan",        "Backout Plan"),
        ("test_plan",           "Test Plan"),
    ]:
        text = _val(item.get(field, ""))
        if text and text.strip():
            lines.append(f"## {label}")
            lines.append("")
            lines.append(text.strip())
            lines.append("")

    # ── All Fields (complete, tabular) ────────────────────────
    lines.append("## All Fields")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    for field_name in sorted(item.keys()):
        disp = _val(item[field_name])
        raw  = _raw(item[field_name])
        cell = f"{disp} *(raw: {raw})*" if (raw and raw != disp) else disp
        # Escape pipe chars inside cell values
        cell = cell.replace("|", "\\|")
        lines.append(f"| `{field_name}` | {cell} |")
    lines.append("")

    # ── Raw JSON ───────────────────────────────────────────────
    lines.append("## Raw JSON")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(item, indent=2, default=str))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# Write records to disk
# ══════════════════════════════════════════════════════════════

def write_records(table: str, records: list, headline_fields: list) -> dict:
    table_dir = KNOWLEDGE_DIR / table
    table_dir.mkdir(parents=True, exist_ok=True)

    written, skipped = 0, 0
    sample_fields    = set()

    for item in records:
        sample_fields.update(item.keys())
        rid = _record_id(item)
        try:
            md   = render_markdown(table, item, headline_fields)
            path = table_dir / f"{rid}.md"
            path.write_text(md, encoding="utf-8")
            written += 1
        except Exception as exc:
            print(f"  WARNING: could not write {rid}: {exc}")
            skipped += 1

    print(f"   ✓ wrote {written} files to knowledge/{table}/  (skipped {skipped})")
    return {"fields": sorted(sample_fields)}


# ══════════════════════════════════════════════════════════════
# Manifest  (read by embedding_builder to validate freshness)
# ══════════════════════════════════════════════════════════════

def write_manifest(results: dict, schema: dict) -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "synced_at": datetime.datetime.utcnow().isoformat() + "Z",
        "base_url":  BASE_URL,
        "tables":    results,
        "schema":    schema,
    }
    path = META_DIR / "manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n✓ Manifest → {path}")


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def main():
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    results, schema = {}, {}

    for table, config in TABLES.items():
        try:
            records          = fetch_all(table, config)
            s                = write_records(table, records, config["headline_fields"])
            results[table]   = {"count": len(records), "status": "ok"}
            schema[table]    = s
        except Exception as exc:
            print(f"\n[ERROR] {table}: {exc}")
            results[table]   = {"count": 0, "status": f"FAILED: {exc}"}

    write_manifest(results, schema)

    print("\n" + "═" * 55)
    print("ServiceNow sync complete")
    print("═" * 55)
    for t, r in results.items():
        status = "✓" if r["status"] == "ok" else "✗"
        print(f"  {status}  {t:<22}  {r['count']:>6} records")
    print("═" * 55)


if __name__ == "__main__":
    main()
