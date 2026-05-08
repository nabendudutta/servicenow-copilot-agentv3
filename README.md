# ServiceNow Copilot Agent v3

Enterprise DevOps AI assistant with **internal-first** vector DB search.

## What Changed (v2 → v3)

| Component | v2 Problem | v3 Fix |
|-----------|-----------|--------|
| `servicenow-copilot.agent.md` | Vague rules — agent ignored internal DB and went to internet | Strict 3-attempt waterfall with mandatory `query_vectordb.py` calls |
| `query_vectordb.py` | **Did not exist** — nothing queried the vector DB | New script: queries FAISS, returns scored results with confidence % |
| `embedding_builder_github.py` | Built FAISS but no keyword index | Now also writes `vectordb/keyword_index.json` for fast pre-screening |
| `internet_search.py` | Did not exist — agent used Copilot's built-in web search freely | New script: only called after 3 internal failures, announces itself |
| `sync-servicenow.yml` | Sync did not rebuild vector DB after update | Now triggers `rebuild-vector` job automatically after every sync |

## How the Search Waterfall Works

```
User Query
    │
    ▼
Attempt 1 — query_vectordb.py "<exact query>" --top_k 5
    │  score >= 0.70? ──────────────────────────────────► Answer (95% internal conf)
    │  no
    ▼
Attempt 2 — query_vectordb.py "<expanded keywords>" --top_k 8
    │  score >= 0.55? ──────────────────────────────────► Answer (65-80% internal conf)
    │  no
    ▼
Attempt 3 — query_vectordb.py "<broad category>" --top_k 10
    │  any results?  ───────────────────────────────────► Answer (30-55% internal conf)
    │  none
    ▼
Announce: "Not found in internal DB after 3 attempts. Searching internet..."
    │
    ▼
internet_search.py "<query>" --max_results 5
    └─────────────────────────────────────────────────► Answer (50-60% internet conf)
```

## Response Header (shown on every answer)

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗄️  Source         : Internal DB ✅
📊  Internal conf  : 87%
🌐  Internet conf  : 0%
🔍  DB Attempts    : 1
📁  Matched files  : INC0001234.md, KB0004521.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Required GitHub Secrets

| Secret | Purpose |
|--------|---------|
| `SNOW_INSTANCE` | ServiceNow instance URL |
| `SNOW_USER` | ServiceNow API username |
| `SNOW_PASSWORD` | ServiceNow API password |
| `GH_PAT` | GitHub PAT for embedding API (github.com → Settings → Tokens) |

## File Structure

```
.github/
  agents/servicenow-copilot.agent.md   ← Agent instructions (3-attempt waterfall)
  workflows/sync-servicenow.yml        ← Sync + auto-rebuild (3x daily)
  workflows/build-vector.yml           ← Standalone vector rebuild

sync/
  servicenow_sync.py                   ← Pulls data from ServiceNow → MD files
  embedding_builder_github.py          ← Builds FAISS + keyword_index.json
  query_vectordb.py                    ← ✅ NEW: Agent queries this for internal search
  internet_search.py                   ← ✅ NEW: Last-resort internet search

knowledge/                             ← Markdown files from ServiceNow
vectordb/                              ← FAISS index + keyword_index.json
```

## Testing the Internal Search Locally

```bash
# Set your GitHub token
export GITHUB_TOKEN=ghp_yourtoken

# Test query against local vector DB
python sync/query_vectordb.py "sonarqube quality gate failure" --top_k 5

# Test with lower threshold to see weak matches
python sync/query_vectordb.py "terraform error" --top_k 8 --threshold 0.3

# Test internet fallback
python sync/internet_search.py "veracode policy compliance fix"
```
