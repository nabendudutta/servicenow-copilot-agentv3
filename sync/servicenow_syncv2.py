import os
import json
import time
import requests
from requests.auth import HTTPBasicAuth

# ============================================
# Environment Variables
# ============================================

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE")
SNOW_USER = os.getenv("SNOW_USER")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD")

if not SNOW_INSTANCE or not SNOW_USER or not SNOW_PASSWORD:
    raise EnvironmentError(
        "Missing required environment variables: "
        "SNOW_INSTANCE, SNOW_USER, SNOW_PASSWORD"
    )

# ============================================
# Normalize URL
# ============================================

if SNOW_INSTANCE.startswith("https://"):
    BASE_URL = SNOW_INSTANCE.rstrip("/")
else:
    BASE_URL = f"https://{SNOW_INSTANCE}.service-now.com"

# ============================================
# ServiceNow Tables to Sync
# No query filters on incident/change_request
# so ALL records are fetched regardless of status
# ============================================

TABLES = {

    "incident": {
        "query": "",           # No filter — fetch ALL incidents (all states)
        "page_size": 1000      # Records per paginated request
    },

    "change_request": {
        "query": "",           # No filter — fetch ALL change orders (all states)
        "page_size": 1000
    },

    "kb_knowledge": {
        "query": "",           # All knowledge articles
        "page_size": 500
    },

    "problem": {
        "query": "",
        "page_size": 500
    },

    "sc_req_item": {
        "query": "",
        "page_size": 500
    },

    "sc_task": {
        "query": "",
        "page_size": 500
    }
}

# ============================================
# Common Headers
# sysparm_display_value=all  → returns BOTH
# raw value and display label for every field
# ============================================

HEADERS = {
    "Accept": "application/json"
}

AUTH = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)

# ============================================
# Retry Configuration
# ============================================

MAX_RETRIES = 3
RETRY_BACKOFF = 2   # seconds (doubles each retry)

# ============================================
# Create Knowledge Directory
# ============================================

os.makedirs("knowledge", exist_ok=True)

# ============================================
# Fetch a Single Page of Records
# ============================================

def fetch_page(table_name, query, limit, offset):
    """
    Fetch one page of results from a ServiceNow table.
    Returns the list of records or raises on error.
    """
    url = f"{BASE_URL}/api/now/table/{table_name}"

    params = {
        "sysparm_query":         query,
        "sysparm_limit":         limit,
        "sysparm_offset":        offset,
        "sysparm_display_value": "all",   # raw + display values for every field
        "sysparm_exclude_reference_link": "false"  # include linked record details
    }

    # Remove empty query so ServiceNow doesn't apply accidental filters
    if not params["sysparm_query"]:
        del params["sysparm_query"]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                auth=AUTH,
                headers=HEADERS,
                params=params,
                timeout=120
            )
            response.raise_for_status()
            return response.json().get("result", [])

        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            print(f"  Attempt {attempt} failed ({e}). Retrying in {wait}s...")
            time.sleep(wait)

# ============================================
# Fetch ALL Records with Pagination
# ============================================

def fetch_all_records(table_name, config):
    """
    Paginate through ALL records in a table using
    sysparm_offset so nothing is missed.
    """
    print(f"\n{'='*50}")
    print(f"Syncing table: {table_name}")
    print(f"{'='*50}")

    all_records = []
    offset = 0
    page_size = config["page_size"]
    query = config.get("query", "")

    while True:
        print(f"  Fetching records {offset + 1} – {offset + page_size}...")

        page = fetch_page(table_name, query, page_size, offset)

        if not page:
            print(f"  No more records. Total fetched: {len(all_records)}")
            break

        all_records.extend(page)
        print(f"  Page returned {len(page)} records. Running total: {len(all_records)}")

        # If the page returned fewer than page_size, we've hit the end
        if len(page) < page_size:
            print(f"  Last page reached. Total fetched: {len(all_records)}")
            break

        offset += page_size

    return all_records

# ============================================
# Write Records to Disk
# Each record → its own .md file with ALL fields
# ============================================

def write_records(table_name, records):
    """
    Write every record as a Markdown file containing
    all fields (both display value and raw value).
    """
    table_dir = f"knowledge/{table_name}"
    os.makedirs(table_dir, exist_ok=True)

    for item in records:

        # Determine a meaningful filename
        record_id = (
            item.get("number", {}).get("value")
            or item.get("number")
            or item.get("name", {}).get("value")
            or item.get("name")
            or item.get("sys_id", {}).get("value")
            or item.get("sys_id")
            or "unknown"
        )

        # ----------------------------------------
        # Build a readable section for every field
        # When sysparm_display_value=all, each field
        # is a dict: {"value": "raw", "display_value": "label"}
        # ----------------------------------------
        field_lines = []
        for field_name, field_data in sorted(item.items()):
            if isinstance(field_data, dict):
                raw   = field_data.get("value", "")
                label = field_data.get("display_value", "")
                if raw == label or not label:
                    field_lines.append(f"**{field_name}:** {raw}")
                else:
                    field_lines.append(
                        f"**{field_name}:** {label}  _(raw: {raw})_"
                    )
            else:
                field_lines.append(f"**{field_name}:** {field_data}")

        fields_block = "\n".join(field_lines)

        content = f"""# {table_name.upper()} — {record_id}

## All Fields

{fields_block}

---

## Full JSON (raw)

```json
{json.dumps(item, indent=2, default=str)}
```
"""

        file_path = f"{table_dir}/{record_id}.md"

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"  Written {len(records)} files to: {table_dir}/")

# ============================================
# Summary Report
# ============================================

def write_summary(results):
    summary_lines = ["# ServiceNow Sync Summary\n"]
    total = 0
    for table, count in results.items():
        summary_lines.append(f"- **{table}**: {count} records")
        total += count
    summary_lines.append(f"\n**Total records synced: {total}**")

    with open("knowledge/SYNC_SUMMARY.md", "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    print("\n" + "\n".join(summary_lines))

# ============================================
# Main Sync Execution
# ============================================

def main():
    sync_results = {}

    for table, config in TABLES.items():
        try:
            records = fetch_all_records(table, config)
            write_records(table, records)
            sync_results[table] = len(records)

        except Exception as e:
            print(f"\n[ERROR] Failed syncing {table}: {e}")
            sync_results[table] = f"FAILED ({e})"

    write_summary(sync_results)
    print("\nServiceNow sync completed.")


if __name__ == "__main__":
    main()