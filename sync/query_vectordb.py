#!/usr/bin/env python3
"""
query_vectordb.py — Internal KB search tool for ServiceNow Copilot agent.

Usage:
    python sync/query_vectordb.py "sonarqube quality gate timeout" --top_k 5
    python sync/query_vectordb.py "terraform state lock" --top_k 8 --threshold 0.55

The agent calls this script for every query before going to the internet.
Output is structured so the agent can parse confidence scores and file names.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

# ── Argument parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Query the internal ServiceNow FAISS vector database")
parser.add_argument("query",         type=str,   help="Search query text")
parser.add_argument("--top_k",       type=int,   default=5,    help="Number of results to return (default: 5)")
parser.add_argument("--threshold",   type=float, default=0.0,  help="Minimum similarity score filter (default: 0.0 = return all)")
parser.add_argument("--vectordb",    type=str,   default="vectordb", help="Path to FAISS vectordb folder")
parser.add_argument("--json",        action="store_true",       help="Output as JSON (default: human-readable)")
args = parser.parse_args()

VECTORDB_PATH = Path(args.vectordb)
QUERY         = args.query.strip()
TOP_K         = args.top_k
THRESHOLD     = args.threshold

# ── Check vectordb exists ─────────────────────────────────────────────────────
if not VECTORDB_PATH.exists() or not any(VECTORDB_PATH.iterdir()):
    msg = {
        "status":   "error",
        "error":    f"Vector database not found at '{VECTORDB_PATH}'. Run the build-vector workflow first.",
        "results":  [],
        "count":    0,
    }
    print(json.dumps(msg, indent=2))
    sys.exit(1)

# ── Load embeddings (same model used to build the DB) ────────────────────────
try:
    from langchain_community.vectorstores import FAISS
    from langchain_openai import OpenAIEmbeddings
    from dotenv import load_dotenv

    load_dotenv()

    github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")

    if not github_token:
        raise EnvironmentError(
            "GITHUB_TOKEN or GH_PAT environment variable is required.\n"
            "Set it in your .env file or GitHub Actions secrets."
        )

    embeddings = OpenAIEmbeddings(
        model      = "text-embedding-3-small",
        api_key    = github_token,
        base_url   = "https://models.inference.ai.azure.com",
    )

    vector_db = FAISS.load_local(
        str(VECTORDB_PATH),
        embeddings,
        allow_dangerous_deserialization=True,
    )

except ImportError as e:
    print(json.dumps({"status": "error", "error": f"Missing dependency: {e}. Run: pip install -r requirements.txt", "results": [], "count": 0}))
    sys.exit(1)

except Exception as e:
    print(json.dumps({"status": "error", "error": str(e), "results": [], "count": 0}))
    sys.exit(1)

# ── Run similarity search ─────────────────────────────────────────────────────
try:
    raw_results = vector_db.similarity_search_with_score(QUERY, k=TOP_K)
except Exception as e:
    print(json.dumps({"status": "error", "error": f"Search failed: {e}", "results": [], "count": 0}))
    sys.exit(1)

# ── Format results ────────────────────────────────────────────────────────────
results = []

for doc, raw_score in raw_results:
    # FAISS returns L2 distance (lower = better). Convert to 0-1 similarity.
    # Score = 1 / (1 + distance) gives a clean 0–1 range.
    similarity = round(1.0 / (1.0 + float(raw_score)), 4)

    if similarity < THRESHOLD:
        continue

    source_path = doc.metadata.get("source", "unknown")
    file_name   = Path(source_path).name
    folder      = Path(source_path).parent.name

    # Derive record type from folder name
    type_map = {
        "incident":       "🚨 Incident",
        "change_request": "🔧 Change",
        "problem":        "🐛 Problem",
        "kb_knowledge":   "📖 Knowledge",
        "sc_req_item":    "📋 Request Item",
        "sc_task":        "✅ Task",
    }
    record_type = type_map.get(folder, f"📁 {folder}")

    # Extract record number from filename (INC0001234 etc.)
    record_number = Path(source_path).stem

    # Confidence label
    if similarity >= 0.85:
        confidence_label = "🟢 High"
        confidence_pct   = int(95 * similarity)
    elif similarity >= 0.70:
        confidence_label = "🟡 Medium"
        confidence_pct   = int(90 * similarity)
    elif similarity >= 0.55:
        confidence_label = "🟠 Low-Medium"
        confidence_pct   = int(80 * similarity)
    else:
        confidence_label = "🔴 Low"
        confidence_pct   = int(60 * similarity)

    results.append({
        "rank":             len(results) + 1,
        "file":             file_name,
        "path":             source_path,
        "record_number":    record_number,
        "record_type":      record_type,
        "similarity_score": similarity,
        "confidence_pct":   confidence_pct,
        "confidence_label": confidence_label,
        "excerpt":          doc.page_content[:400].strip(),
    })

# ── Determine overall status ──────────────────────────────────────────────────
best_score = results[0]["similarity_score"] if results else 0.0

if best_score >= 0.70:
    status = "found_high_confidence"
elif best_score >= 0.55:
    status = "found_low_confidence"
elif results:
    status = "found_weak_match"
else:
    status = "not_found"

output = {
    "status":       status,
    "query":        QUERY,
    "top_k":        TOP_K,
    "count":        len(results),
    "best_score":   best_score,
    "searched_at":  datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    "vectordb":     str(VECTORDB_PATH),
    "results":      results,
}

# ── Print output ──────────────────────────────────────────────────────────────
if args.json:
    print(json.dumps(output, indent=2))
else:
    # Human-readable format (what the agent reads)
    print("=" * 60)
    print(f"🔍 INTERNAL DB SEARCH RESULTS")
    print(f"   Query   : {QUERY}")
    print(f"   Status  : {status}")
    print(f"   Found   : {len(results)} result(s)")
    print(f"   Best    : {best_score:.4f} similarity score")
    print("=" * 60)

    if not results:
        print("\n❌ No results found in internal database.")
        print("   → Proceed to next search attempt or internet fallback.\n")
    else:
        for r in results:
            print(f"\n📄 [{r['rank']}] {r['record_type']} — {r['record_number']}")
            print(f"   File       : {r['file']}")
            print(f"   Similarity : {r['similarity_score']:.4f}  {r['confidence_label']}  ({r['confidence_pct']}%)")
            print(f"   Excerpt    :")
            for line in r["excerpt"].splitlines()[:6]:
                print(f"     {line}")
            print()

    print("=" * 60)
    print(f"Searched: {output['searched_at']}")
    print("=" * 60)
