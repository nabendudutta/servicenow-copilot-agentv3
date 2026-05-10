#!/usr/bin/env python3
"""
query_vectordb.py
─────────────────
CLI search tool called automatically by the GitHub Copilot agent.
Searches the FAISS vector database built from ServiceNow knowledge files.

Designed for SILENT AUTO-EXECUTION — clean output, meaningful exit codes.

Usage
─────
  python sync/query_vectordb.py "<query>" [options]

Options
───────
  --top_k N          Number of results to return (default: 10)
  --min_score F      Minimum similarity score 0.0–1.0 (default: 0.30)
  --filter KEY=VAL   Metadata filter, e.g. --filter table=incident
  --section NAME     Only return chunks from this section
                     Values: summary, description, resolution, all_fields
  --json             Output raw JSON (for programmatic use)

Examples
────────
  python sync/query_vectordb.py "Terraform state lock Azure" --top_k 10
  python sync/query_vectordb.py "INC0012345" --top_k 3 --min_score 0.50
  python sync/query_vectordb.py "state lock fix" --filter table=incident --section resolution
  python sync/query_vectordb.py "P1 network outage" --filter table=incident --top_k 10
  python sync/query_vectordb.py "failed apply subnet" --min_score 0.35 --top_k 10

Exit codes
──────────
  0  — one or more results found above min_score
  1  — no results found (agent should run next search step)
  2  — system error (DB missing, import error, bad args)
"""

import os
import sys
import json
import argparse
from pathlib import Path

# ── Dependency guard ──────────────────────────────────────────────────────────
try:
    from langchain_community.vectorstores import FAISS
    from langchain_openai import OpenAIEmbeddings
    from dotenv import load_dotenv
except ImportError as e:
    print(f"[SYSTEM ERROR] Missing dependency: {e}")
    print("Run: pip install langchain-community langchain-openai python-dotenv faiss-cpu")
    sys.exit(2)

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
VECTORDB_DIR  = Path("vectordb")
KEYWORD_INDEX = VECTORDB_DIR / "keyword_index.json"

# ── Section name aliases (user-friendly → stored metadata value) ──────────────
SECTION_ALIASES = {
    "resolution":   ["resolution notes", "resolution", "close notes", "close_notes"],
    "description":  ["description", "short description"],
    "summary":      ["summary"],
    "all_fields":   ["all fields"],
    "plans":        ["implementation plan", "backout plan", "test plan"],
}

# ── Token ─────────────────────────────────────────────────────────────────────
github_token = (
    os.getenv("GITHUB_TOKEN")
    or os.getenv("GH_PAT")
    or os.getenv("OPENAI_API_KEY")
)
if not github_token:
    print("[SYSTEM ERROR] No API token. Set GITHUB_TOKEN, GH_PAT, or OPENAI_API_KEY.")
    sys.exit(2)

use_github_models = bool(os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT"))

# ── Argument parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Search the ServiceNow internal FAISS vector database",
    add_help=True
)
parser.add_argument("query",       type=str,   help="Search query")
parser.add_argument("--top_k",     type=int,   default=10)
parser.add_argument("--min_score", type=float, default=0.30)
parser.add_argument("--filter",    type=str,   default=None,
                    help="Metadata filter: field=value (e.g. table=incident)")
parser.add_argument("--section",   type=str,   default=None,
                    help="Section filter: resolution | description | summary | all_fields")
parser.add_argument("--json",      action="store_true")
args = parser.parse_args()

# ── Validate DB ───────────────────────────────────────────────────────────────
if not VECTORDB_DIR.exists() or not (VECTORDB_DIR / "index.faiss").exists():
    print("[SYSTEM ERROR] Vector DB not found at vectordb/")
    print("               Run: python sync/embedding_builder_github.py")
    sys.exit(2)

# ── Parse --filter ────────────────────────────────────────────────────────────
meta_filter = None
if args.filter:
    try:
        key, val = args.filter.split("=", 1)
        meta_filter = {key.strip(): val.strip()}
    except ValueError:
        print(f"[WARN] Invalid --filter '{args.filter}' — expected field=value. Ignored.")

# ── Load FAISS ────────────────────────────────────────────────────────────────
embeddings = OpenAIEmbeddings(
    model    = "text-embedding-3-small",
    api_key  = github_token,
    base_url = "https://models.inference.ai.azure.com" if use_github_models else None,
)

try:
    vector_db = FAISS.load_local(
        str(VECTORDB_DIR),
        embeddings,
        allow_dangerous_deserialization=True,
    )
except Exception as e:
    print(f"[SYSTEM ERROR] Failed to load FAISS index: {e}")
    sys.exit(2)

# ── Keyword pre-screen ────────────────────────────────────────────────────────
# Fast exact-match lookup using keyword_index.json before vector search.
# Especially useful for INC/CHG/PRB number lookups.

keyword_hits = []

if KEYWORD_INDEX.exists():
    try:
        index_data   = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        query_lower  = args.query.lower()
        query_tokens = set(query_lower.split())

        # Choose entries: filter by table if --filter table= is set
        entries = index_data.get("entries", [])
        if meta_filter and "table" in meta_filter:
            entries = [e for e in entries if e.get("table") == meta_filter["table"]]

        for entry in entries:
            rid  = (entry.get("record_id") or "").lower()
            kws  = set(entry.get("keywords", []))
            flds = " ".join([
                entry.get("state",    ""),
                entry.get("priority", ""),
                entry.get("category", ""),
                entry.get("severity", ""),
                entry.get("urgency",  ""),
            ]).lower()

            # Exact record number match
            if rid and rid in query_lower:
                keyword_hits.insert(0, entry)
                continue

            # Structured field token match
            long_tokens = [tok for tok in query_tokens if len(tok) > 3]
            field_match = any(tok in flds for tok in long_tokens)

            # Keyword overlap (need 2+ to avoid noise)
            overlap = query_tokens & kws
            if len(overlap) >= 2 or field_match:
                keyword_hits.append(entry)

    except Exception as e:
        print(f"[WARN] Keyword index error: {e}")


# ── Vector search ─────────────────────────────────────────────────────────────
# If --section is specified, we request more results and post-filter.
fetch_k = args.top_k * 3 if args.section else args.top_k

try:
    if meta_filter:
        raw_results = vector_db.similarity_search_with_score(
            args.query, k=fetch_k, filter=meta_filter
        )
    else:
        raw_results = vector_db.similarity_search_with_score(
            args.query, k=fetch_k
        )
except Exception as e:
    print(f"[SYSTEM ERROR] Vector search failed: {e}")
    sys.exit(2)

# ── Score normalisation (FAISS L2 → 0–1) ─────────────────────────────────────
def norm(dist: float) -> float:
    return max(0.0, min(1.0, 1.0 - (dist / 2.0)))

# ── Section filter ────────────────────────────────────────────────────────────
def matches_section(section_meta: str, requested: str) -> bool:
    if not requested:
        return True
    aliases = SECTION_ALIASES.get(requested.lower(), [requested.lower()])
    s = (section_meta or "").lower()
    return any(alias in s for alias in aliases)

# ── Build results list ────────────────────────────────────────────────────────
results = []
seen_record_ids = set()    # de-duplicate: keep best score per record+section

for doc, dist in raw_results:
    score    = norm(dist)
    section  = doc.metadata.get("section", "")
    rec_id   = doc.metadata.get("record_id", "")

    if score < args.min_score:
        continue

    if args.section and not matches_section(section, args.section):
        continue

    dedup_key = f"{rec_id}::{section}"
    if dedup_key in seen_record_ids:
        continue
    seen_record_ids.add(dedup_key)

    results.append((doc, score))

results.sort(key=lambda x: x[1], reverse=True)
results = results[:args.top_k]

# ── Confidence label ──────────────────────────────────────────────────────────
def conf_label(score: float) -> str:
    if score >= 0.85: return "✅ HIGH (95%)"
    if score >= 0.70: return "✅ GOOD (80%)"
    if score >= 0.55: return "⚠️  MODERATE (65%)"
    if score >= 0.40: return "⚠️  WEAK (50%)"
    if score >= 0.25: return "❌ VERY WEAK (30%)"
    return "❌ BELOW THRESHOLD"

# ── No results ────────────────────────────────────────────────────────────────
if not results and not keyword_hits:
    if not args.json:
        print(f"[NO RESULTS] '{args.query}'")
        if meta_filter:
            print(f"             filter : {meta_filter}")
        if args.section:
            print(f"             section: {args.section}")
        print(f"             min_score threshold: {args.min_score}")
        print("             Try: lower --min_score, remove --filter, or broaden query")
    else:
        print(json.dumps({"query": args.query, "result_count": 0,
                          "results": [], "keyword_hits": []}, indent=2))
    sys.exit(1)

# ── JSON output ───────────────────────────────────────────────────────────────
if args.json:
    output = {
        "query":        args.query,
        "filter":       meta_filter,
        "section":      args.section,
        "result_count": len(results),
        "keyword_hit_count": len(keyword_hits),
        "results": [
            {
                "rank":       i + 1,
                "score":      round(s, 4),
                "confidence": conf_label(s),
                "record_id":  d.metadata.get("record_id", ""),
                "table":      d.metadata.get("table", ""),
                "section":    d.metadata.get("section", ""),
                "state":      d.metadata.get("state", ""),
                "priority":   d.metadata.get("priority", ""),
                "category":   d.metadata.get("category", ""),
                "severity":   d.metadata.get("severity", ""),
                "urgency":    d.metadata.get("urgency", ""),
                "impact":     d.metadata.get("impact", ""),
                "opened_at":  d.metadata.get("opened_at", ""),
                "updated_at": d.metadata.get("updated_at", ""),
                "file":       d.metadata.get("file", ""),
                # change_request extras
                "change_type": d.metadata.get("change_type", ""),
                "phase":       d.metadata.get("phase", ""),
                "risk":        d.metadata.get("risk", ""),
                "content":     d.page_content[:800],
            }
            for i, (d, s) in enumerate(results)
        ],
        "keyword_candidates": [
            {
                "record_id": e.get("record_id"),
                "table":     e.get("table"),
                "state":     e.get("state"),
                "priority":  e.get("priority"),
                "file":      e.get("file"),
                "excerpt":   e.get("excerpt", "")[:300],
            }
            for e in keyword_hits[:5]
        ],
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)

# ── Human-readable output ─────────────────────────────────────────────────────
W = 66

print()
print("━" * W)
print(f"  INTERNAL DB SEARCH RESULTS")
print(f"  Query       : {args.query}")
if meta_filter:
    print(f"  Filter      : {meta_filter}")
if args.section:
    print(f"  Section     : {args.section}")
print(f"  Results     : {len(results)} match(es)   |   "
      f"keyword pre-screen: {len(keyword_hits)}")
print("━" * W)

for rank, (doc, score) in enumerate(results, 1):
    m = doc.metadata

    print(f"\n  ── RESULT {rank} ─── {conf_label(score)}  (score {score:.3f})")
    print(f"  Record   : {m.get('record_id', 'N/A')}")
    print(f"  Table    : {m.get('table',     'N/A')}")
    print(f"  Section  : {m.get('section',   'N/A')}")
    print(f"  State    : {m.get('state',     'N/A')}")
    print(f"  Priority : {m.get('priority',  'N/A')}")
    print(f"  Category : {m.get('category',  'N/A')}")

    tbl = m.get("table", "")
    if tbl == "incident":
        print(f"  Severity : {m.get('severity', 'N/A')}")
        print(f"  Urgency  : {m.get('urgency',  'N/A')}")
        print(f"  Impact   : {m.get('impact',   'N/A')}")
    if tbl == "change_request":
        print(f"  CHG Type : {m.get('change_type', 'N/A')}")
        print(f"  Phase    : {m.get('phase',       'N/A')}")
        print(f"  Risk     : {m.get('risk',        'N/A')}")

    print(f"  Opened   : {m.get('opened_at',  'N/A')}")
    print(f"  Updated  : {m.get('updated_at', 'N/A')}")
    print(f"  File     : {m.get('file',        'N/A')}")
    print()

    # Content preview — show full content for resolution sections
    content = doc.page_content.strip()
    sec_lower = (m.get("section") or "").lower()
    is_resolution = any(k in sec_lower for k in ["resolution", "close", "root cause"])
    preview_len = len(content) if is_resolution else 700

    for line in content[:preview_len].splitlines():
        print(f"  {line}")
    if len(content) > preview_len:
        print(f"  ... [{len(content) - preview_len} more chars — open {m.get('file')} for full content]")
    print()
    print("  " + "─" * (W - 2))

# ── Keyword-only hits (shown when vector search found nothing) ────────────────
if keyword_hits and not results:
    print(f"\n  KEYWORD PRE-SCREEN CANDIDATES")
    print(f"  (No vector score — these matched on structured fields / keywords)")
    print("  " + "─" * (W - 2))
    for e in keyword_hits[:5]:
        print(f"  Record   : {e.get('record_id')}  [{e.get('table')}]")
        print(f"  State    : {e.get('state')}   Priority: {e.get('priority')}")
        print(f"  Excerpt  : {e.get('excerpt', '')[:250]}")
        print(f"  File     : {e.get('file')}")
        print()

print("━" * W)
print(f"  DB path: {VECTORDB_DIR}/   min_score: {args.min_score}")
print("━" * W)
sys.exit(0)