#!/usr/bin/env python3
"""
embedding_builder_github.py
Builds the FAISS vector database from all Markdown files in knowledge/
and writes a rich keyword + metadata index to vectordb/keyword_index.json.

Design goals
────────────
1. Every FAISS chunk carries full metadata (table, record_id, state,
   priority, section) so the Copilot agent can pre-filter before
   semantic search — dramatically improving precision.

2. Chunk boundaries respect logical document sections (## headings),
   so a chunk never splits a field table mid-row or a description
   mid-sentence.

3. The keyword index is enriched with ITSM-specific structured fields
   extracted from YAML front-matter, making keyword pre-screen
   accurate for queries like "P1 incidents" or "failed change requests".

4. The FAISS index is always rebuilt from scratch on every run
   (no stale chunks from previous syncs).
"""

import os
import re
import json
import time
import datetime
import yaml
from pathlib import Path

from langchain_community.document_loaders import TextLoader
from langchain.schema import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════
# Paths & constants
# ══════════════════════════════════════════════════════════════

KNOWLEDGE_DIR = Path("knowledge")
VECTORDB_DIR  = Path("vectordb")

# Chunk sizes tuned for ITSM records:
# - Primary splitter uses Markdown headers → keeps Summary, Description,
#   Resolution, All Fields as separate coherent chunks.
# - Fallback splitter handles any section that is still too long.
FALLBACK_CHUNK_SIZE    = 1200   # characters (raised from 500 — avoids splitting mid-field)
FALLBACK_CHUNK_OVERLAP = 200

EMBED_BATCH_SIZE  = 50
EMBED_SLEEP       = 0.5         # seconds between batches (rate-limit guard)
EMBED_RETRY_SLEEP = 10

# ITSM stop-words — terms that appear in every record and add no signal
STOP_WORDS = {
    "that", "this", "with", "from", "have", "will", "were", "been",
    "your", "they", "when", "what", "which", "also", "more", "than",
    "then", "into", "some", "none", "true", "false",
    # Generic ITSM structure words (present in every file — no signal)
    "table", "record", "field", "value", "summary", "description",
    "section", "json", "raw", "fields", "markdown",
}

# ══════════════════════════════════════════════════════════════
# GitHub Models token
# ══════════════════════════════════════════════════════════════

github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")
if not github_token:
    raise ValueError(
        "GITHUB_TOKEN / GH_PAT is missing.\n"
        "GitHub Actions: pass via step env from secret GH_PAT.\n"
        "Local: set GITHUB_TOKEN in your .env file."
    )
print(f"GitHub token present (length={len(github_token)})")

# ══════════════════════════════════════════════════════════════
# YAML front-matter parser
# ══════════════════════════════════════════════════════════════

def extract_frontmatter(text: str) -> dict:
    """
    Pull the YAML block between the first two '---' delimiters.
    Returns an empty dict if none found or parse fails.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


# ══════════════════════════════════════════════════════════════
# Section-aware Markdown splitter
#
# Strategy:
#  1. Split on ## headings first → each logical section becomes
#     a chunk (Summary, Description, Resolution, All Fields, etc.)
#  2. Any section chunk that exceeds FALLBACK_CHUNK_SIZE is then
#     split by the recursive character splitter.
#  3. Front-matter metadata is attached to EVERY sub-chunk so the
#     agent always knows the table, record_id, state, priority.
# ══════════════════════════════════════════════════════════════

HEADER_SPLITTER = MarkdownHeaderTextSplitter(
    headers_to_split_on=[
        ("#",  "title"),
        ("##", "section"),
    ],
    strip_headers=False,
)

FALLBACK_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=FALLBACK_CHUNK_SIZE,
    chunk_overlap=FALLBACK_CHUNK_OVERLAP,
    separators=["\n\n", "\n", " ", ""],
)


def split_document(path: Path, text: str, fm: dict) -> list[Document]:
    """
    Return a list of LangChain Documents, each with metadata:
      table, record_id, state, priority, category,
      section, file, opened_at, updated_at
    """
    base_meta = {
        "table":      fm.get("table",     path.parent.name),
        "record_id":  fm.get("record_id", path.stem),
        "sys_id":     fm.get("sys_id",    ""),
        "state":      fm.get("state",     ""),
        "priority":   fm.get("priority",  ""),
        "category":   fm.get("category",  ""),
        "opened_at":  fm.get("opened_at", ""),
        "updated_at": fm.get("updated_at",""),
        "file":       str(path),
        # change_request extras
        "change_type": fm.get("change_type", ""),
        "phase":       fm.get("phase",       ""),
        "risk":        fm.get("risk",        ""),
        # incident extras
        "severity":    fm.get("severity",    ""),
        "urgency":     fm.get("urgency",     ""),
        "impact":      fm.get("impact",      ""),
    }

    # Section-level split
    header_chunks = HEADER_SPLITTER.split_text(text)

    docs = []
    for chunk in header_chunks:
        section = (
            chunk.metadata.get("section")
            or chunk.metadata.get("title")
            or "body"
        )
        chunk_meta = {**base_meta, "section": section}
        chunk_text = chunk.page_content.strip()
        if not chunk_text:
            continue

        # Skip the Raw JSON section — too noisy for semantic search;
        # it's only useful for exact-match which keyword index covers.
        if section.lower() in ("raw json", "all fields"):
            # Still include but as a single non-split chunk
            docs.append(Document(page_content=chunk_text, metadata=chunk_meta))
            continue

        # Fallback-split if this section is too long
        if len(chunk_text) > FALLBACK_CHUNK_SIZE:
            sub_chunks = FALLBACK_SPLITTER.split_text(chunk_text)
            for sub in sub_chunks:
                if sub.strip():
                    docs.append(Document(page_content=sub.strip(),
                                         metadata=chunk_meta))
        else:
            docs.append(Document(page_content=chunk_text, metadata=chunk_meta))

    return docs


# ══════════════════════════════════════════════════════════════
# Keyword index builder
#
# Extracts structured ITSM fields AND free-text keywords so the
# agent can pre-screen candidates before hitting the vector index.
# ══════════════════════════════════════════════════════════════

def build_keyword_entry(path: Path, text: str, fm: dict) -> dict:
    # Free-text keywords from the body (excluding front-matter)
    body  = re.sub(r'^---.*?---\s*', '', text, flags=re.DOTALL)
    words = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-]{3,}\b', body.lower())
    kws   = list(dict.fromkeys(w for w in words if w not in STOP_WORDS))[:60]

    # Short description is the best single-sentence summary
    excerpt = ""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 20:
            excerpt = line[:300]
            break

    return {
        "file":       str(path),
        "record_id":  fm.get("record_id", path.stem),
        "table":      fm.get("table",     path.parent.name),
        "sys_id":     fm.get("sys_id",    ""),
        # Structured fields — agent can filter on these directly
        "state":      fm.get("state",     ""),
        "priority":   fm.get("priority",  ""),
        "category":   fm.get("category",  ""),
        "severity":   fm.get("severity",  ""),
        "urgency":    fm.get("urgency",   ""),
        "impact":     fm.get("impact",    ""),
        "change_type": fm.get("change_type", ""),
        "phase":      fm.get("phase",     ""),
        "risk":       fm.get("risk",      ""),
        "opened_at":  fm.get("opened_at", ""),
        "updated_at": fm.get("updated_at",""),
        "size_chars": len(text),
        "keywords":   kws,
        "excerpt":    excerpt,
    }


# ══════════════════════════════════════════════════════════════
# Embedding helpers
# ══════════════════════════════════════════════════════════════

def batch_list(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def embed_all(chunks: list[Document], embeddings) -> FAISS:
    """Embed in batches with retry; always rebuilds from scratch."""
    vector_db    = None
    total        = -(-len(chunks) // EMBED_BATCH_SIZE)

    for i, batch in enumerate(batch_list(chunks, EMBED_BATCH_SIZE)):
        print(f"  Embedding batch {i+1}/{total} ({len(batch)} chunks)…",
              end=" ", flush=True)
        for attempt in range(1, 4):
            try:
                if vector_db is None:
                    vector_db = FAISS.from_documents(batch, embeddings)
                else:
                    vector_db.add_documents(batch)
                print("ok")
                break
            except Exception as exc:
                print(f"attempt {attempt} failed: {exc}", end=" ")
                if attempt == 3:
                    print("SKIPPED")
                else:
                    time.sleep(EMBED_RETRY_SLEEP)
        time.sleep(EMBED_SLEEP)

    return vector_db


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def main():
    # ── Load & parse all Markdown files ───────────────────────
    all_chunks    = []
    keyword_index = []
    skipped       = []

    md_files = sorted(KNOWLEDGE_DIR.rglob("*.md"))
    print(f"Found {len(md_files)} Markdown files in {KNOWLEDGE_DIR}/")

    for path in md_files:
        # Skip the _meta directory (manifest, not ITSM records)
        if "_meta" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            fm   = extract_frontmatter(text)

            chunks = split_document(path, text, fm)
            all_chunks.extend(chunks)

            kw_entry = build_keyword_entry(path, text, fm)
            keyword_index.append(kw_entry)

            print(f"  ✓  {path}  ({len(chunks)} chunks)")
        except Exception as exc:
            print(f"  ✗  {path}: {exc}")
            skipped.append(str(path))

    if not all_chunks:
        raise ValueError(
            "No chunks produced. Check that knowledge/ contains .md files "
            "from servicenow_sync.py."
        )

    print(f"\nTotal documents : {len(keyword_index)}")
    print(f"Total chunks    : {len(all_chunks)}")
    if skipped:
        print(f"Skipped files   : {len(skipped)}")

    # ── Embed ──────────────────────────────────────────────────
    embeddings = OpenAIEmbeddings(
        model    = "text-embedding-3-small",
        api_key  = github_token,
        base_url = "https://models.inference.ai.azure.com",
    )

    print(f"\nStarting embedding ({len(all_chunks)} chunks)…")
    vector_db = embed_all(all_chunks, embeddings)

    if vector_db is None:
        raise RuntimeError("All embedding batches failed — vector DB is empty.")

    # ── Save FAISS ─────────────────────────────────────────────
    VECTORDB_DIR.mkdir(exist_ok=True)
    vector_db.save_local(str(VECTORDB_DIR))
    print(f"\n✓ FAISS vector DB saved → {VECTORDB_DIR}/")

    # ── Save keyword + metadata index ─────────────────────────
    # Group entries by table for faster agent pre-filtering
    by_table: dict[str, list] = {}
    for entry in keyword_index:
        by_table.setdefault(entry["table"], []).append(entry)

    index_payload = {
        "built_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "doc_count":   len(keyword_index),
        "chunk_count": len(all_chunks),
        "tables":      list(by_table.keys()),
        "by_table":    by_table,           # keyed by table name for O(1) lookup
        "entries":     keyword_index,       # flat list for backward compat
    }

    index_path = VECTORDB_DIR / "keyword_index.json"
    index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
    print(f"✓ Keyword index saved → {index_path}  ({len(keyword_index)} entries)")

    # ── Summary ────────────────────────────────────────────────
    print("\n" + "═" * 55)
    print("Vector DB build complete")
    print("═" * 55)
    for tbl, entries in by_table.items():
        print(f"  {tbl:<22}  {len(entries):>6} records indexed")
    print(f"\n  Total chunks embedded: {len(all_chunks)}")
    print("═" * 55)


if __name__ == "__main__":
    main()
