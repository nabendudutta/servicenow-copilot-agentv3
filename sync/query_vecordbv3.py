#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_vectordb.py
-----------------
CLI search tool called automatically by the GitHub Copilot agent.
Searches the FAISS vector database built by embedding_builder_githubv3.py.

Embedding model
---------------
Uses the SAME local HuggingFace model as the builder:
  sentence-transformers/all-MiniLM-L6-v2

This is CRITICAL -- the query embedding model MUST match the build model
exactly or FAISS will return meaningless results.

No OpenAI / GitHub Models API calls are made for search.
The GitHub token is only used by the Copilot agent for final answer
generation, never for embedding or vector search.

Usage
-----
  python sync/query_vectordb.py "<query>" [options]

Options
-------
  --top_k N          Number of results (default: 10)
  --min_score F      Minimum similarity 0.0-1.0 (default: 0.30)
  --filter KEY=VAL   Metadata filter e.g. --filter table=incident
  --section NAME     Section filter: resolution|description|summary|all_fields
  --json             Raw JSON output

Examples
--------
  python sync/query_vectordb.py "Terraform state lock Azure" --top_k 10
  python sync/query_vectordb.py "INC0012345"
  python sync/query_vectordb.py "state lock fix" --filter table=incident --section resolution
  python sync/query_vectordb.py "P1 network outage" --filter table=incident
  python sync/query_vectordb.py "failed apply subnet" --min_score 0.35

Exit codes
----------
  0  -- results found above min_score
  1  -- no results found (agent runs next search step)
  2  -- system error (DB missing, model load failed, bad args)
"""

import os
import sys
import json
import argparse
from pathlib import Path

# -- Dependency guard -----------------------------------------------------
try:
    from langchain_community.vectorstores import FAISS
    from langchain_huggingface import HuggingFaceEmbeddings
    from dotenv import load_dotenv
except ImportError as e:
    print(f"[SYSTEM ERROR] Missing dependency: {e}")
    print("Run: pip install langchain-community langchain-huggingface "
          "sentence-transformers faiss-cpu python-dotenv")
    sys.exit(2)

load_dotenv()

# -- Paths ----------------------------------------------------------------
VECTORDB_DIR  = Path("vectordb")
KEYWORD_INDEX = VECTORDB_DIR / "keyword_index.json"
HF_CACHE_DIR  = Path(".hf_cache")

# -- Embedding model -- MUST match embedding_builder_githubv3.py ----------
HF_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# -- Section aliases -------------------------------------------------------
SECTION_ALIASES = {
    "resolution":  ["resolution notes", "resolution", "close notes",
                    "close_notes"],
    "description": ["description", "short description"],
    "summary":     ["summary"],
    "all_fields":  ["all fields"],
    "plans":       ["implementation plan", "backout plan", "test plan"],
}

# -- Argument parsing ------------------------------------------------------
parser = argparse.ArgumentParser(
    description="Search the ServiceNow internal FAISS vector database"
)
parser.add_argument("query",       type=str,   help="Search query")
parser.add_argument("--top_k",     type=int,   default=10)
parser.add_argument("--min_score", type=float, default=0.30)
parser.add_argument("--filter",    type=str,   default=None,
                    help="field=value e.g. table=incident")
parser.add_argument("--section",   type=str,   default=None,
                    help="resolution|description|summary|all_fields")
parser.add_argument("--json",      action="store_true")
args = parser.parse_args()

# -- Validate DB exists ----------------------------------------------------
if not VECTORDB_DIR.exists() or not (VECTORDB_DIR / "index.faiss").exists():
    print("[SYSTEM ERROR] Vector DB not found at vectordb/")
    print("               Run: python sync/embedding_builder_githubv3.py")
    sys.exit(2)

# -- Verify embedding model matches build model ----------------------------
# Warn if keyword_index.json records a different model name
if KEYWORD_INDEX.exists():
    try:
        idx_meta = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        built_with = idx_meta.get("embedding_model", "")
        if built_with and built_with != HF_MODEL_NAME:
            print(f"[WARN] Model mismatch!")
            print(f"       DB built with : {built_with}")
            print(f"       Query using   : {HF_MODEL_NAME}")
            print(f"       Results may be inaccurate. Rebuild the DB.")
    except Exception:
        pass

# -- Parse --filter --------------------------------------------------------
meta_filter = None
if args.filter:
    try:
        key, val = args.filter.split("=", 1)
        meta_filter = {key.strip(): val.strip()}
    except ValueError:
        print(f"[WARN] Invalid --filter '{args.filter}' -- ignored.")

# -- Load local HuggingFace embedding model --------------------------------
# Uses same model as the builder -- no API calls, no token needed
try:
    embeddings = HuggingFaceEmbeddings(
        model_name    = HF_MODEL_NAME,
        cache_folder  = str(HF_CACHE_DIR),
        model_kwargs  = {"device": "cpu"},
        encode_kwargs = {"normalize_embeddings": True},
    )
except Exception as e:
    print(f"[SYSTEM ERROR] Failed to load embedding model: {e}")
    print(f"               Run: pip install sentence-transformers")
    sys.exit(2)

# -- Load FAISS index ------------------------------------------------------
try:
    vector_db = FAISS.load_local(
        str(VECTORDB_DIR),
        embeddings,
        allow_dangerous_deserialization=True,
    )
except Exception as e:
    print(f"[SYSTEM ERROR] Failed to load FAISS index: {e}")
    sys.exit(2)

# -- Keyword pre-screen ----------------------------------------------------
# Fast structured lookup using keyword_index.json before vector search.
# Handles exact INC/CHG/PRB number lookups and structured field filters.

keyword_hits = []

if KEYWORD_INDEX.exists():
    try:
        index_data   = json.loads(KEYWORD_INDEX.read_text(encoding="utf-8"))
        query_lower  = args.query.lower()
        query_tokens = set(query_lower.split())

        entries = index_data.get("entries", [])
        if meta_filter and "table" in meta_filter:
            entries = [e for e in entries
                       if e.get("table") == meta_filter["table"]]

        for entry in entries:
            rid  = (entry.get("record_id") or "").lower()
            kws  = set(k.lower() for k in entry.get("keywords", []))
            flds = " ".join([
                entry.get("state",    ""),
                entry.get("priority", ""),
                entry.get("category", ""),
                entry.get("severity", ""),
                entry.get("urgency",  ""),
            ]).lower()

            # Exact record number match -- highest priority
            if rid and rid in query_lower:
                keyword_hits.insert(0, entry)
                continue

            # Structured field token match
            long_tokens = [t for t in query_tokens if len(t) > 3]
            field_match = any(t in flds for t in long_tokens)

            # Keyword overlap (2+ to avoid noise)
            overlap = query_tokens & kws
            if len(overlap) >= 2 or field_match:
                keyword_hits.append(entry)

    except Exception as e:
        print(f"[WARN] Keyword index error: {e}")

# -- Vector similarity search ----------------------------------------------
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

# -- Score normalisation ---------------------------------------------------
# With normalize_embeddings=True in both builder and query, FAISS uses
# inner product which equals cosine similarity (range 0-1, higher=better).
# FAISS returns NEGATIVE inner product as "distance", so we negate it.
def norm(dist):
    # For IndexFlatIP (inner product): distance is already the similarity
    # For IndexFlatL2: convert with 1 - dist/2
    # HuggingFace normalised embeddings use IP, so scores are 0-1 directly
    score = 1.0 - (dist / 2.0)   # safe for both index types
    return max(0.0, min(1.0, score))

# -- Section filter --------------------------------------------------------
def matches_section(section_meta, requested):
    if not requested:
        return True
    aliases = SECTION_ALIASES.get(requested.lower(), [requested.lower()])
    s = (section_meta or "").lower()
    return any(alias in s for alias in aliases)

# -- Build result list (dedup by record+section) ---------------------------
results = []
seen    = set()

for doc, dist in raw_results:
    score   = norm(dist)
    section = doc.metadata.get("section", "")
    rec_id  = doc.metadata.get("record_id", "")

    if score < args.min_score:
        continue
    if args.section and not matches_section(section, args.section):
        continue

    key = f"{rec_id}::{section}"
    if key in seen:
        continue
    seen.add(key)
    results.append((doc, score))

results.sort(key=lambda x: x[1], reverse=True)
results = results[:args.top_k]

# -- Confidence label ------------------------------------------------------
def conf_label(score):
    if score >= 0.85: return "[OK] HIGH (95%)"
    if score >= 0.70: return "[OK] GOOD (80%)"
    if score >= 0.55: return "[!]  MODERATE (65%)"
    if score >= 0.40: return "[!]  WEAK (50%)"
    if score >= 0.25: return "[X]  VERY WEAK (30%)"
    return "[X]  BELOW THRESHOLD"

# -- No results ------------------------------------------------------------
if not results and not keyword_hits:
    if not args.json:
        print(f"[NO RESULTS] '{args.query}'")
        if meta_filter:
            print(f"             filter  : {meta_filter}")
        if args.section:
            print(f"             section : {args.section}")
        print(f"             min_score: {args.min_score}")
        print("             Try: lower --min_score, remove --filter, "
              "or broaden query")
    else:
        print(json.dumps({
            "query": args.query, "result_count": 0,
            "results": [], "keyword_hits": [],
        }, indent=2))
    sys.exit(1)

# -- JSON output -----------------------------------------------------------
if args.json:
    output = {
        "query":             args.query,
        "filter":            meta_filter,
        "section":           args.section,
        "embedding_model":   HF_MODEL_NAME,
        "result_count":      len(results),
        "keyword_hit_count": len(keyword_hits),
        "results": [
            {
                "rank":        i + 1,
                "score":       round(s, 4),
                "confidence":  conf_label(s),
                "record_id":   d.metadata.get("record_id",   ""),
                "table":       d.metadata.get("table",       ""),
                "section":     d.metadata.get("section",     ""),
                "state":       d.metadata.get("state",       ""),
                "priority":    d.metadata.get("priority",    ""),
                "category":    d.metadata.get("category",    ""),
                "severity":    d.metadata.get("severity",    ""),
                "urgency":     d.metadata.get("urgency",     ""),
                "impact":      d.metadata.get("impact",      ""),
                "opened_at":   d.metadata.get("opened_at",   ""),
                "updated_at":  d.metadata.get("updated_at",  ""),
                "file":        d.metadata.get("file",        ""),
                "change_type": d.metadata.get("change_type", ""),
                "phase":       d.metadata.get("phase",       ""),
                "risk":        d.metadata.get("risk",        ""),
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

# -- Human-readable output -------------------------------------------------
W = 66

print()
print("=" * W)
print("  INTERNAL DB SEARCH RESULTS")
print(f"  Query        : {args.query}")
print(f"  Model        : {HF_MODEL_NAME} (local)")
if meta_filter:
    print(f"  Filter       : {meta_filter}")
if args.section:
    print(f"  Section      : {args.section}")
print(f"  Results      : {len(results)} match(es)  |  "
      f"keyword pre-screen: {len(keyword_hits)}")
print("=" * W)

for rank, (doc, score) in enumerate(results, 1):
    m = doc.metadata

    print(f"\n  -- RESULT {rank} --- {conf_label(score)}  "
          f"(score {score:.3f})")
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

    # Show full content for resolution sections, preview for others
    content    = doc.page_content.strip()
    sec_lower  = (m.get("section") or "").lower()
    is_res     = any(k in sec_lower
                     for k in ["resolution", "close", "root cause"])
    preview    = len(content) if is_res else 700

    for line in content[:preview].splitlines():
        print(f"  {line}")
    if len(content) > preview:
        print(f"  ... [{len(content) - preview} more chars -- "
              f"open {m.get('file')} for full content]")
    print()
    print("  " + "-" * (W - 2))

# -- Keyword-only hits (when vector search found nothing) ------------------
if keyword_hits and not results:
    print("\n  KEYWORD PRE-SCREEN CANDIDATES")
    print("  (No vector score -- matched on structured fields / keywords)")
    print("  " + "-" * (W - 2))
    for e in keyword_hits[:5]:
        print(f"  Record   : {e.get('record_id')}  [{e.get('table')}]")
        print(f"  State    : {e.get('state')}   "
              f"Priority: {e.get('priority')}")
        print(f"  Excerpt  : {e.get('excerpt', '')[:250]}")
        print(f"  File     : {e.get('file')}")
        print()

print("=" * W)
print(f"  DB: {VECTORDB_DIR}/   model: {HF_MODEL_NAME}   "
      f"min_score: {args.min_score}")
print("=" * W)
sys.exit(0)
