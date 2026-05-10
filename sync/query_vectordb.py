#!/usr/bin/env python3
"""
query_vectordb.py
─────────────────
CLI tool used by the GitHub Copilot agent to search the FAISS vector
database built from ServiceNow knowledge files.

Usage
─────
  python sync/query_vectordb.py "<query>" [--top_k N] [--filter field=value] [--min_score F]

Examples
────────
  python sync/query_vectordb.py "Terraform state lock Azure" --top_k 10
  python sync/query_vectordb.py "P1 network outage" --top_k 5 --filter table=incident
  python sync/query_vectordb.py "known error workaround" --top_k 8 --filter table=problem
  python sync/query_vectordb.py "INC0012345"
  python sync/query_vectordb.py "failed deployment" --min_score 0.40

Exit codes
──────────
  0  — results found and printed
  1  — no results found (agent should try next strategy)
  2  — vector DB not found (needs rebuild)
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
    print(f"[ERROR] Missing dependency: {e}")
    print("Run: pip install langchain-community langchain-openai python-dotenv faiss-cpu")
    sys.exit(2)

load_dotenv()

VECTORDB_DIR  = Path("vectordb")
KEYWORD_INDEX = VECTORDB_DIR / "keyword_index.json"

# ── Token resolution ──────────────────────────────────────────────────────────
github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT") or os.getenv("OPENAI_API_KEY")
if not github_token:
    print("[ERROR] No API token found. Set GITHUB_TOKEN, GH_PAT, or OPENAI_API_KEY.")
    sys.exit(2)

# ── Detect whether we are using GitHub Models or OpenAI directly ──────────────
use_github_models = bool(os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT"))

# ── Argument parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Search the ServiceNow FAISS vector DB")
parser.add_argument("query",       type=str,   help="Search query string")
parser.add_argument("--top_k",     type=int,   default=5,    help="Number of results (default 5)")
parser.add_argument("--min_score", type=float, default=0.30, help="Minimum similarity score 0-1 (default 0.30)")
parser.add_argument("--filter",    type=str,   default=None,
                    help="Metadata filter as field=value (e.g. table=incident)")
parser.add_argument("--json",      action="store_true", help="Output raw JSON instead of formatted text")
args = parser.parse_args()

# ── Validate DB exists ────────────────────────────────────────────────────────
if not VECTORDB_DIR.exists() or not (VECTORDB_DIR / "index.faiss").exists():
    print(f"[ERROR] Vector DB not found at {VECTORDB_DIR}/")
    print("        Run: python sync/embedding_builder_github.py")
    sys.exit(2)

# ── Parse --filter argument ───────────────────────────────────────────────────
meta_filter = None
if args.filter:
    try:
        key, val = args.filter.split("=", 1)
        meta_filter = {key.strip(): val.strip()}
    except ValueError:
        print(f"[WARN] Invalid --filter format '{args.filter}' — expected field=value. Ignoring.")

# ── Load embeddings & FAISS ───────────────────────────────────────────────────
embeddings = OpenAIEmbeddings(
    model    = "text-embedding-3-small",
    api_key  = github_token,
    base_url = "https://models.inference.ai.azure.com" if use_github_models else None,
)

try:
    vector_db = FAISS.load_local(
        str(VECTORDB_DIR),
        embeddings,
        allow_dangerous_deserialization=True
    )
except Exception as e:
    print(f"[ERROR] Failed to load FAISS index: {e}")
    sys.exit(2)

# ── Keyword pre-screen ────────────────────────────────────────────────────────
# Use keyword_index.json to find candidate record IDs before vector search.
# This boosts recall for exact record number lookups (INC/CHG/PRB).
keyword_hits = []

if KEYWORD_INDEX.exists():
    try:
        index_data = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        query_lower = args.query.lower()
        query_tokens = set(query_lower.split())

        entries = index_data.get("entries", [])

        # If filter specifies a table, restrict to that table's entries
        if meta_filter and "table" in meta_filter:
            target_table = meta_filter["table"]
            entries = [e for e in entries if e.get("table") == target_table]

        for entry in entries:
            rid = entry.get("record_id", "").lower()
            kws = set(entry.get("keywords", []))
            state    = entry.get("state", "").lower()
            priority = entry.get("priority", "").lower()
            category = entry.get("category", "").lower()

            # Exact record number match — highest priority
            if rid and rid in query_lower:
                keyword_hits.insert(0, entry)
                continue

            # Structured field match (state, priority, category)
            field_match = any([
                tok in state    for tok in query_tokens,
                tok in priority for tok in query_tokens,
                tok in category for tok in query_tokens,
            ]) if query_tokens else False

            # Keyword overlap
            overlap = query_tokens & kws
            if len(overlap) >= 2 or field_match:
                keyword_hits.append(entry)

        if keyword_hits:
            print(f"[keyword pre-screen] {len(keyword_hits)} candidate(s) found")
    except Exception as e:
        print(f"[WARN] Keyword index read failed: {e}")

# ── Vector similarity search ──────────────────────────────────────────────────
try:
    if meta_filter:
        raw_results = vector_db.similarity_search_with_score(
            args.query,
            k=args.top_k,
            filter=meta_filter,
        )
    else:
        raw_results = vector_db.similarity_search_with_score(
            args.query,
            k=args.top_k,
        )
except Exception as e:
    print(f"[ERROR] Vector search failed: {e}")
    sys.exit(2)

# ── FAISS returns L2 distance; convert to 0-1 cosine-like score ──────────────
# Lower L2 distance = more similar. We normalise to a 0-1 score.
def normalise_score(l2_distance: float) -> float:
    # Heuristic: distance 0 → score 1.0, distance 2 → score 0.0
    return max(0.0, 1.0 - (l2_distance / 2.0))

results = []
for doc, dist in raw_results:
    score = normalise_score(dist)
    if score >= args.min_score:
        results.append((doc, score))

# Sort descending by score
results.sort(key=lambda x: x[1], reverse=True)

# ── Output ────────────────────────────────────────────────────────────────────
if not results and not keyword_hits:
    print(f"[NO RESULTS] Query: '{args.query}'")
    if meta_filter:
        print(f"             Filter: {meta_filter}")
    print(f"             Min score threshold: {args.min_score}")
    print("             Try lowering --min_score or removing --filter")
    sys.exit(1)

if args.json:
    # Machine-readable output for programmatic use
    output = {
        "query":          args.query,
        "filter":         meta_filter,
        "result_count":   len(results),
        "keyword_hits":   len(keyword_hits),
        "results": [
            {
                "score":     round(score, 4),
                "record_id": doc.metadata.get("record_id", ""),
                "table":     doc.metadata.get("table", ""),
                "state":     doc.metadata.get("state", ""),
                "priority":  doc.metadata.get("priority", ""),
                "section":   doc.metadata.get("section", ""),
                "file":      doc.metadata.get("file", ""),
                "content":   doc.page_content[:500],
            }
            for doc, score in results
        ],
        "keyword_candidates": [
            {
                "record_id": e.get("record_id"),
                "table":     e.get("table"),
                "state":     e.get("state"),
                "priority":  e.get("priority"),
                "excerpt":   e.get("excerpt", "")[:200],
            }
            for e in keyword_hits[:5]
        ],
    }
    print(json.dumps(output, indent=2))
    sys.exit(0)

# ── Human-readable output ─────────────────────────────────────────────────────
print()
print("━" * 65)
print(f"  INTERNAL DB SEARCH RESULTS")
print(f"  Query   : {args.query}")
if meta_filter:
    print(f"  Filter  : {meta_filter}")
print(f"  Results : {len(results)} vector match(es)  |  "
      f"{len(keyword_hits)} keyword pre-screen hit(s)")
print("━" * 65)

if results:
    for rank, (doc, score) in enumerate(results, 1):
        meta = doc.metadata
        pct  = round(score * 100, 1)

        # Confidence label
        if score >= 0.85:   label = "✅ HIGH"
        elif score >= 0.70: label = "✅ GOOD"
        elif score >= 0.55: label = "⚠️  MODERATE"
        elif score >= 0.40: label = "⚠️  WEAK"
        else:               label = "❌ VERY WEAK"

        print(f"\n  [{rank}] {label}  —  score {pct}%")
        print(f"       Record   : {meta.get('record_id', 'N/A')}")
        print(f"       Table    : {meta.get('table', 'N/A')}")
        print(f"       State    : {meta.get('state', 'N/A')}")
        print(f"       Priority : {meta.get('priority', 'N/A')}")
        print(f"       Section  : {meta.get('section', 'N/A')}")
        print(f"       File     : {meta.get('file', 'N/A')}")
        print(f"       Category : {meta.get('category', 'N/A')}")
        if meta.get('table') == 'incident':
            print(f"       Severity : {meta.get('severity', 'N/A')}")
            print(f"       Urgency  : {meta.get('urgency', 'N/A')}")
        if meta.get('table') == 'change_request':
            print(f"       Type     : {meta.get('change_type', 'N/A')}")
            print(f"       Phase    : {meta.get('phase', 'N/A')}")
            print(f"       Risk     : {meta.get('risk', 'N/A')}")
        print(f"       Opened   : {meta.get('opened_at', 'N/A')}")
        print(f"       Updated  : {meta.get('updated_at', 'N/A')}")
        print()
        # Show the matched content excerpt
        content_preview = doc.page_content.strip()[:600]
        for line in content_preview.splitlines():
            print(f"       {line}")
        print()
        print("  " + "─" * 63)

else:
    print("\n  No vector matches above score threshold.")

# ── Keyword pre-screen hits (supplemental) ────────────────────────────────────
if keyword_hits and not results:
    print("\n  KEYWORD PRE-SCREEN CANDIDATES (no vector score — check these manually):")
    print("  " + "─" * 63)
    for entry in keyword_hits[:5]:
        print(f"  Record  : {entry.get('record_id')}  [{entry.get('table')}]")
        print(f"  State   : {entry.get('state')}   Priority: {entry.get('priority')}")
        print(f"  Excerpt : {entry.get('excerpt', '')[:200]}")
        print(f"  File    : {entry.get('file')}")
        print()

print("━" * 65)
print(f"  Searched: {VECTORDB_DIR}/  |  Min score: {args.min_score}")
print("━" * 65)
sys.exit(0)
