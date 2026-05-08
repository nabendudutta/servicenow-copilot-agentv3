import os
import time
import requests
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

# ============================================
# Step 1: Fetch Live Incidents from ServiceNow
# ============================================
SN_INSTANCE = os.getenv("SN_INSTANCE")   # e.g. https://dev12345.service-now.com
SN_USER     = os.getenv("SN_USERNAME")
SN_PASS     = os.getenv("SN_PASSWORD")

def fetch_incidents():
    url = f"{SN_INSTANCE}/api/now/table/incident"
    params = {
        "sysparm_limit": 500,
        "sysparm_query": "active=true^ORDERBYDESCsys_created_on",
        "sysparm_fields": (
            "number,short_description,description,"
            "close_notes,state,priority,category,"
            "assignment_group,sys_created_on,resolved_at"
        ),
    }
    headers = {"Accept": "application/json"}
    response = requests.get(
        url, auth=(SN_USER, SN_PASS), headers=headers, params=params
    )
    response.raise_for_status()
    return response.json().get("result", [])

print("Fetching incidents from ServiceNow...")
incidents = fetch_incidents()
print(f"Fetched {len(incidents)} incidents.")

# Write each incident as a .md file in knowledge/
os.makedirs("knowledge", exist_ok=True)

for inc in incidents:
    number = inc.get("number", "UNKNOWN")
    filename = f"knowledge/{number}.md"
    content = f"""# {number}: {inc.get('short_description', '')}

**State:** {inc.get('state', '')}
**Priority:** {inc.get('priority', '')}
**Category:** {inc.get('category', '')}
**Assignment Group:** {inc.get('assignment_group', {}).get('display_value', '')}
**Created:** {inc.get('sys_created_on', '')}
**Resolved:** {inc.get('resolved_at', '')}

## Description
{inc.get('description', 'N/A')}

## Close Notes
{inc.get('close_notes', 'N/A')}
"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)

print(f"Written {len(incidents)} incident files to knowledge/")

# ============================================
# Step 2: Load & Embed (your original logic)
# ============================================
documents = []
for root, dirs, files in os.walk("knowledge"):
    for file in files:
        if file.endswith(".md"):
            path = os.path.join(root, file)
            try:
                loader = TextLoader(path, encoding="utf-8")
                documents.extend(loader.load())
            except Exception as e:
                print(f"Failed loading {path}: {e}")

if not documents:
    raise ValueError("No markdown files found.")

splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
chunks = splitter.split_documents(documents)
print(f"Created {len(chunks)} chunks")

github_token = os.getenv("GITHUB_TOKEN")
if not github_token:
    raise ValueError("GITHUB_TOKEN is missing.")

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=github_token,
    base_url="https://models.inference.ai.azure.com"
)

BATCH_SIZE = 50
def batch_chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

vector_db = None
for i, batch in enumerate(batch_chunks(chunks, BATCH_SIZE)):
    print(f"Embedding batch {i+1} ({len(batch)} chunks)...")
    if vector_db is None:
        vector_db = FAISS.from_documents(batch, embeddings)
    else:
        vector_db.add_documents(batch)
    time.sleep(0.5)

vector_db.save_local("vectordb")
print("Vector DB updated successfully with latest incidents.")
