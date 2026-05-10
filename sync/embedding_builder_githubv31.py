#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
embedding_builder_githubv3.py
Builds the FAISS vector database from all Markdown files in knowledge/
and writes a rich keyword + metadata index to vectordb/keyword_index.json.

Embedding model
---------------
Uses HuggingFace sentence-transformers running LOCALLY on the GitHub
Actions runner (CPU). Zero API calls. Zero rate limits. Zero daily caps.

  Model : sentence-transformers/all-MiniLM-L6-v2
  Size  : ~90 MB (downloaded once, cached automatically)
  Speed : ~1000-2000 chunks/min on CPU
  Dims  : 384

OpenAI / GitHub Models token is NOT required by this script.
It is still used by the Copilot agent for final answer generation,
but never for embedding or vector search.

Design goals
------------
1. Every FAISS chunk carries full metadata (table, record_id, state,
   priority, section) so the Copilot agent can pre-filter before
   semantic search.
2. Chunk boundaries respect Markdown ## headings -- no mid-field splits.
3. Keyword index has ITSM-specific structured fields for fast pre-screen.
4. FAISS index always rebuilt from scratch on every run.

Install dependencies
--------------------
  pip install sentence-transformers langchain-huggingface langchain-community
              langchain-text-splitters faiss-cpu pyyaml python-dotenv
"""

import os
import re
import json
import time
import datetime
import yaml
from pathlib import Path

from langchain.schema import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------
# Paths & constants
# -----------------------------------------------------------------------

KNOWLEDGE_DIR = Path("knowledge")
VECTORDB_DIR  = Path("vectordb")

# HuggingFace model -- runs 100% locally, no API calls
# all-MiniLM-L6-v2: best balance of speed, size, and quality for ITSM text
HF_MODEL_NAME  = "sentence-transformers/all-MiniLM-L6-v2"
HF_CACHE_DIR   = Path(".hf_cache")   # local cache so model is not re-downloaded

# Chunk sizes tuned for ITSM records
FALLBACK_CHUNK_SIZE    = 1200
FALLBACK_CHUNK_OVERLAP = 200

# Batch size for FAISS ingestion (not API calls -- purely memory tuning)
# Larger batches are fine since there is no rate limit
EMBED_BATCH_SIZE = 200

# ITSM stop-words -- present in every record, add no search signal
STOP_WORDS = {
    "that", "this", "with", "from", "have", "will", "were", "been",
    "your", "they", "when", "what", "which", "also", "more", "than",
    "then", "into", "some", "none", "true", "false",
    "table", "record", "field", "value", "summary", "description",
    "section", "json", "raw", "fields", "markdown",
}

# -----------------------------------------------------------------------
# YAML front-matter parser
# -----------------------------------------------------------------------

def extract_frontmatter(text):
    """Extract YAML block between first two --- delimiters."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}

# -----------------------------------------------------------------------
# Section-aware Markdown splitter
#
# 1. Split on ## headings first -- Summary, Description, Resolution Notes,
#    All Fields each become their own chunk with full metadata attached.
# 2. Sections longer than FALLBACK_CHUNK_SIZE are further split by the
#    recursive character splitter.
# 3. Every sub-chunk carries table, record_id, state, priority, section
#    so the agent can filter before semantic search.
# -----------------------------------------------------------------------

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


def split_document(path, text, fm):
    """
    Split one Markdown file into LangChain Documents.
    Every Document carries full ITSM metadata for agent pre-filtering.
    """
    base_meta = {
        "table":       fm.get("table",       path.parent.name),
        "record_id":   fm.get("record_id",   path.stem),
        "sys_id":      fm.get("sys_id",      ""),
        "state":       fm.get("state",       ""),
        "priority":    fm.get("priority",    ""),
        "category":    fm.get("category",    ""),
        "opened_at":   fm.get("opened_at",   ""),
        "updated_at":  fm.get("updated_at",  ""),
        "file":        str(path),
        # change_request extras
        "change_type": fm.get("change_type", ""),
        "phase":       fm.get("phase",       ""),
        "risk":        fm.get("risk",        ""),
        # incident extras
        "severity":    fm.get("severity",    ""),
        "urgency":     fm.get("urgency",     ""),
        "impact":      fm.get("impact",      ""),
    }

    docs = []
    for chunk in HEADER_SPLITTER.split_text(text):
        section    = (chunk.metadata.get("section")
                      or chunk.metadata.get("title")
                      or "body")
        chunk_meta = {**base_meta, "section": section}
        chunk_text = chunk.page_content.strip()
        if not chunk_text:
            continue

        # Raw JSON / All Fields: keep as single chunk (exact-match use only)
        if section.lower() in ("raw json", "all fields"):
            docs.append(Document(page_content=chunk_text, metadata=chunk_meta))
            continue

        # Fallback-split oversized sections
        if len(chunk_text) > FALLBACK_CHUNK_SIZE:
            for sub in FALLBACK_SPLITTER.split_text(chunk_text):
                if sub.strip():
                    docs.append(Document(
                        page_content=sub.strip(),
                        metadata=chunk_meta,
                    ))
        else:
            docs.append(Document(page_content=chunk_text, metadata=chunk_meta))

    return docs

# -----------------------------------------------------------------------
# Keyword index entry builder
# -----------------------------------------------------------------------

def build_keyword_entry(path, text, fm):
    """
    Build a keyword index entry for fast pre-screen before vector search.
    Includes structured ITSM fields AND free-text keywords.
    """
    body  = re.sub(r'^---.*?---\s*', '', text, flags=re.DOTALL)
    words = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-]{3,}\b', body.lower())
    kws   = list(dict.fromkeys(w for w in words if w not in STOP_WORDS))[:60]

    excerpt = ""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 20:
            excerpt = line[:300]
            break

    return {
        "file":        str(path),
        "record_id":   fm.get("record_id",   path.stem),
        "table":       fm.get("table",        path.parent.name),
        "sys_id":      fm.get("sys_id",       ""),
        "state":       fm.get("state",        ""),
        "priority":    fm.get("priority",     ""),
        "category":    fm.get("category",     ""),
        "severity":    fm.get("severity",     ""),
        "urgency":     fm.get("urgency",      ""),
        "impact":      fm.get("impact",       ""),
        "change_type": fm.get("change_type",  ""),
        "phase":       fm.get("phase",        ""),
        "risk":        fm.get("risk",         ""),
        "opened_at":   fm.get("opened_at",    ""),
        "updated_at":  fm.get("updated_at",   ""),
        "size_chars":  len(text),
        "keywords":    kws,
        "excerpt":     excerpt,
        # Store the model name so query_vectordb.py can verify it matches
        "embedding_model": HF_MODEL_NAME,
    }

# -----------------------------------------------------------------------
# Batch helper
# -----------------------------------------------------------------------

def batch_list(lst, size):
    for i in range(0, len(lst), size):
        yield i, lst[i:i + size]

# -----------------------------------------------------------------------
# Embedding loop (no rate limits -- runs at full CPU speed)
# -----------------------------------------------------------------------

def embed_all(chunks, embeddings):
    """
    Embed all chunks into FAISS using local HuggingFace model.
    No API calls, no rate limits, no retries needed.
    Processes EMBED_BATCH_SIZE chunks at a time to manage memory.
    """
    vector_db     = None
    total_batches = -(-len(chunks) // EMBED_BATCH_SIZE)

    print(f"[INFO] Embedding {len(chunks)} chunks in {total_batches} "
          f"batches of {EMBED_BATCH_SIZE} (local CPU, no API calls)")

    t_start = time.time()

    for batch_idx, batch in batch_list(chunks, EMBED_BATCH_SIZE):
        pct = int(((batch_idx) / len(chunks)) * 100)
        print(f"  [EMBED] batch {batch_idx // EMBED_BATCH_SIZE + 1}/"
              f"{total_batches}  ({len(batch)} chunks)  {pct}% ...",
              end=" ", flush=True)
        try:
            if vector_db is None:
                vector_db = FAISS.from_documents(batch, embeddings)
            else:
                vector_db.add_documents(batch)
            print("[OK]")
        except Exception as exc:
            print(f"[FAIL] {exc}")

    elapsed = time.time() - t_start
    print(f"[INFO] Embedding complete in {elapsed:.1f}s "
          f"({len(chunks)/elapsed:.0f} chunks/sec)")
    return vector_db

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    HF_CACHE_DIR.mkdir(exist_ok=True)

    # -- Load HuggingFace embedding model --------------------------------
    # Model is downloaded once to HF_CACHE_DIR and cached for subsequent runs.
    # On GitHub Actions this is fast because runners have internet access.
    # On a private runner the model can be pre-downloaded and committed.
    print(f"[INFO] Loading local embedding model: {HF_MODEL_NAME}")
    print(f"[INFO] Cache dir: {HF_CACHE_DIR}")
    print(f"[INFO] No API key required -- fully local inference")

    embeddings = HuggingFaceEmbeddings(
        model_name       = HF_MODEL_NAME,
        cache_folder     = str(HF_CACHE_DIR),
        model_kwargs     = {"device": "cpu"},
        encode_kwargs    = {"normalize_embeddings": True},
        # normalize_embeddings=True converts L2 distance to cosine similarity,
        # which means FAISS scores are directly interpretable as 0-1 similarity
    )
    print(f"[OK] Model loaded")

    # -- Load & split all Markdown files ---------------------------------
    all_chunks    = []
    keyword_index = []
    skipped       = []

    md_files = sorted(KNOWLEDGE_DIR.rglob("*.md"))
    print(f"\n[INFO] Found {len(md_files)} Markdown files in {KNOWLEDGE_DIR}/")

    for path in md_files:
        if "_meta" in path.parts:
            continue
        try:
            text     = path.read_text(encoding="utf-8", errors="ignore")
            fm       = extract_frontmatter(text)
            chunks   = split_document(path, text, fm)
            all_chunks.extend(chunks)
            keyword_index.append(build_keyword_entry(path, text, fm))
            print(f"  [OK] {path}  ({len(chunks)} chunks)")
        except Exception as exc:
            print(f"  [FAIL] {path}: {exc}")
            skipped.append(str(path))

    if not all_chunks:
        raise ValueError(
            "No chunks produced. "
            "Check that knowledge/ contains .md files from servicenow_sync.py."
        )

    print(f"\n[INFO] Documents      : {len(keyword_index)}")
    print(f"[INFO] Total chunks   : {len(all_chunks)}")
    print(f"[INFO] Skipped files  : {len(skipped)}")

    # -- Embed into FAISS ------------------------------------------------
    print()
    vector_db = embed_all(all_chunks, embeddings)

    if vector_db is None:
        raise RuntimeError(
            "[ERROR] No vectors were produced. Check the knowledge/ directory."
        )

    # -- Save FAISS index ------------------------------------------------
    VECTORDB_DIR.mkdir(exist_ok=True)
    vector_db.save_local(str(VECTORDB_DIR))
    print(f"\n[OK] FAISS vector DB saved -> {VECTORDB_DIR}/")

    # -- Save keyword + metadata index -----------------------------------
    by_table = {}
    for entry in keyword_index:
        by_table.setdefault(entry["table"], []).append(entry)

    index_payload = {
        "built_at":        datetime.datetime.utcnow().isoformat() + "Z",
        "embedding_model": HF_MODEL_NAME,   # stored so query script can verify
        "doc_count":       len(keyword_index),
        "chunk_count":     len(all_chunks),
        "tables":          list(by_table.keys()),
        "by_table":        by_table,
        "entries":         keyword_index,
    }

    index_path = VECTORDB_DIR / "keyword_index.json"
    index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
    print(f"[OK] Keyword index saved -> {index_path} "
          f"({len(keyword_index)} entries)")

    # -- Summary ---------------------------------------------------------
    print("\n" + "=" * 55)
    print("Vector DB build complete")
    print(f"Model : {HF_MODEL_NAME} (local, no API)")
    print("=" * 55)
    for tbl, entries in by_table.items():
        print(f"  {tbl:<22}  {len(entries):>6} records indexed")
    print(f"\n  Total chunks embedded : {len(all_chunks)}")
    print("=" * 55)


if __name__ == "__main__":
    main()
