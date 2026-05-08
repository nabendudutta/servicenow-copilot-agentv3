#!/usr/bin/env python3
"""
embedding_builder_github.py
Builds the FAISS vector database from all Markdown files in knowledge/
and writes a lightweight keyword index to vectordb/keyword_index.json.

The keyword index lets the agent do a fast pre-screen before embedding search.
"""

import os
import re
import json
import time
import datetime
from pathlib import Path

from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

KNOWLEDGE_DIR = Path("knowledge")
VECTORDB_DIR  = Path("vectordb")
BATCH_SIZE    = 50

# ── Validate token ────────────────────────────────────────────────────────────
github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_PAT")

if not github_token:
    raise ValueError(
        "GITHUB_TOKEN is None or empty.\n"
        "In GitHub Actions: ensure secret GH_PAT exists and is passed via step env.\n"
        "Locally: ensure GITHUB_TOKEN is set in your .env file."
    )

print(f"GITHUB_TOKEN present, length={len(github_token)}")

# ── Load all Markdown files ───────────────────────────────────────────────────
documents     = []
keyword_index = []

STOP_WORDS = {"that","this","with","from","have","will","were","been","your",
              "they","when","what","which","also","more","than","then","into",
              "some","none","true","false","date","time","last","next","field",
              "value","table","record","short","description","category","state"}

for path in sorted(KNOWLEDGE_DIR.rglob("*.md")):
    try:
        loader = TextLoader(str(path), encoding="utf-8")
        docs   = loader.load()
        documents.extend(docs)

        text     = path.read_text(encoding="utf-8", errors="ignore")
        words    = re.findall(r'\b[A-Za-z][A-Za-z0-9_\-]{3,}\b', text.lower())
        unique_kw = list(dict.fromkeys(w for w in words if w not in STOP_WORDS))[:40]

        keyword_index.append({
            "file":     str(path),
            "name":     path.stem,
            "folder":   path.parent.name,
            "size":     len(text),
            "keywords": unique_kw,
            "excerpt":  text[:200].replace("\n", " ").strip(),
        })
        print(f"  Loaded: {path} ({len(text)} chars)")
    except Exception as e:
        print(f"  WARNING: Failed loading {path}: {e}")

if not documents:
    raise ValueError("No markdown files found in knowledge/ — cannot build vector DB.")

print(f"\nTotal documents loaded: {len(documents)}")

# ── Split into chunks ─────────────────────────────────────────────────────────
splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
chunks   = splitter.split_documents(documents)
print(f"Created {len(chunks)} chunks")

# ── Embed with GitHub Models API ──────────────────────────────────────────────
embeddings = OpenAIEmbeddings(
    model    = "text-embedding-3-small",
    api_key  = github_token,
    base_url = "https://models.inference.ai.azure.com",
)

def batch_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

vector_db     = None
total_batches = -(-len(chunks) // BATCH_SIZE)

for i, batch in enumerate(batch_list(chunks, BATCH_SIZE)):
    print(f"  Embedding batch {i+1}/{total_batches} ({len(batch)} chunks)...")
    try:
        if vector_db is None:
            vector_db = FAISS.from_documents(batch, embeddings)
        else:
            vector_db.add_documents(batch)
        time.sleep(0.5)
    except Exception as e:
        print(f"  WARNING: Batch {i+1} failed: {e} — retrying in 5s...")
        time.sleep(5)
        if vector_db is None:
            vector_db = FAISS.from_documents(batch, embeddings)
        else:
            vector_db.add_documents(batch)

# ── Save FAISS DB ─────────────────────────────────────────────────────────────
VECTORDB_DIR.mkdir(exist_ok=True)
vector_db.save_local(str(VECTORDB_DIR))
print(f"\nFAISS Vector DB saved to {VECTORDB_DIR}/")

# ── Save keyword index ────────────────────────────────────────────────────────
index_payload = {
    "built_at":    datetime.datetime.utcnow().isoformat(),
    "doc_count":   len(documents),
    "chunk_count": len(chunks),
    "entries":     keyword_index,
}
index_path = VECTORDB_DIR / "keyword_index.json"
index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
print(f"Keyword index saved to {index_path} ({len(keyword_index)} entries)")

print("\nVector database build complete.")
