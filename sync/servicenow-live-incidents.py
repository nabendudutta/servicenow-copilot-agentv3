import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta, timezone

# ============================================
# Environment Variables
# ============================================

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE")
SNOW_USER     = os.getenv("SNOW_USER")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD")

# ============================================
# Normalize URL
# ============================================

if SNOW_INSTANCE.startswith("https://"):
    BASE_URL = SNOW_INSTANCE.rstrip("/")
else:
    BASE_URL = f"https://{SNOW_INSTANCE}.service-now.com"

# ============================================
# Dynamic date filter — last 30 days
# (change the timedelta value to adjust window)
# ============================================

SINCE_DATE = (
    datetime.now(timezone.utc) - timedelta(days=30)
).strftime("%Y-%m-%d %H:%M:%S")

# ============================================
# ServiceNow Tables to Sync
# ============================================
# KEY FIX: incident query changed from
#   "active=false"  (closed/old only)
# to:
#   "active=true"          → all open/live incidents
#   "ORsys_created_on>="   → plus recently created ones
#   "ORresolved_at>="      → plus recently resolved ones
# This ensures NEW incidents are always captured.
# ============================================

TABLES = {

    "incident": {
        # Fetch: all active incidents OR any created/resolved in last 30 days
        "query": (
            f"active=true"
            f"^ORsys_created_on>={SINCE_DATE}"
            f"^ORresolved_at>={SINCE_DATE}"
        ),
        "limit": 1000,
        "fields": (
            "number,short_description,description,close_notes,"
            "state,priority,urgency,impact,category,subcategory,"
            "assignment_group,assigned_to,caller_id,"
            "sys_created_on,opened_at,resolved_at,closed_at,"
            "cmdb_ci,comments_and_work_notes,close_code"
        )
    },

    "kb_knowledge": {
        "query": "workflow_state=published",
        "limit": 200,
        "fields": (
            "number,short_description,text,category,"
            "sys_created_on,sys_updated_on,author"
        )
    },

    "problem": {
        "query": f"sys_created_on>={SINCE_DATE}^ORactive=true",
        "limit": 200,
        "fields": (
            "number,short_description,description,state,"
            "priority,category,assignment_group,sys_created_on"
        )
    },

    "change_request": {
        "query": f"sys_created_on>={SINCE_DATE}^ORactive=true",
        "limit": 200,
        "fields": (
            "number,short_description,description,state,"
            "priority,category,assignment_group,sys_created_on,"
            "start_date,end_date,close_notes"
        )
    },

    "sc_req_item": {
        "query": f"sys_created_on>={SINCE_DATE}^ORactive=true",
        "limit": 200,
        "fields": (
            "number,short_description,description,state,"
            "priority,cat_item,sys_created_on,assignment_group"
        )
    },

    "sc_task": {
        "query": f"sys_created_on>={SINCE_DATE}^ORactive=true",
        "limit": 200,
        "fields": (
            "number,short_description,description,state,"
            "priority,sys_created_on,assignment_group"
        )
    }
}

# ============================================
# Common Headers & Auth
# ============================================

HEADERS = {"Accept": "application/json"}
AUTH    = HTTPBasicAuth(SNOW_USER, SNOW_PASSWORD)

# ============================================
# Create Knowledge Directory
# ============================================

os.makedirs("knowledge", exist_ok=True)

# ============================================
# Helper: safely extract display_value or raw
# ============================================

def extract(field):
    """Handle both plain strings and {'value':..., 'display_value':...} dicts."""
    if isinstance(field, dict):
        return field.get("display_value") or field.get("value") or ""
    return field or ""

# ============================================
# Fetch Records with Pagination
# ============================================

def fetch_table_data(table_name, config):
    print(f"\n{'='*50}")
    print(f"Syncing table : {table_name}")
    print(f"Query         : {config['query']}")
    print(f"Since         : {SINCE_DATE}")
    print(f"{'='*50}")

    url     = f"{BASE_URL}/api/now/table/{table_name}"
    limit   = config["limit"]
    fields  = config.get("fields", "")
    offset  = 0
    total_fetched = 0
    all_results   = []

    # Paginate until all records are retrieved
    while True:
        params = {
            "sysparm_query":        config["query"],
            "sysparm_limit":        min(limit, 200),   # max 200 per page
            "sysparm_offset":       offset,
            "sysparm_display_value": "all",            # get both value & display_value
            "sysparm_exclude_reference_link": "true",  # cleaner output
        }
        if fields:
            params["sysparm_fields"] = fields

        try:
            response = requests.get(
                url,
                auth=AUTH,
                headers=HEADERS,
                params=params,
                timeout=120
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"  HTTP Error on {table_name}: {e}")
            break
        except requests.exceptions.ConnectionError as e:
            print(f"  Connection Error on {table_name}: {e}")
            break

        results = response.json().get("result", [])
        if not results:
            break

        all_results.extend(results)
        total_fetched += len(results)
        offset        += len(results)

        print(f"  Fetched {total_fetched} records so far...")

        # Stop if we got fewer than page size or hit the limit
        if len(results) < 200 or total_fetched >= limit:
            break

    print(f"  Total fetched from {table_name}: {total_fetched}")

    # ==========================================
    # Write each record as a Markdown file
    # ==========================================
    table_dir = f"knowledge/{table_name}"
    os.makedirs(table_dir, exist_ok=True)

    written = 0
    for item in all_results:
        record_number = (
            extract(item.get("number"))
            or extract(item.get("name"))
            or item.get("sys_id", "unknown")
        )

        # ---- Incident-specific extra fields ----
        close_notes  = extract(item.get("close_notes", ""))
        close_code   = extract(item.get("close_code", ""))
        resolved_at  = extract(item.get("resolved_at", ""))
        closed_at    = extract(item.get("closed_at", ""))
        opened_at    = extract(item.get("opened_at", ""))
        urgency      = extract(item.get("urgency", ""))
        impact       = extract(item.get("impact", ""))
        caller       = extract(item.get("caller_id", ""))
        assigned_to  = extract(item.get("assigned_to", ""))
        cmdb_ci      = extract(item.get("cmdb_ci", ""))
        subcategory  = extract(item.get("subcategory", ""))
        work_notes   = extract(item.get("comments_and_work_notes", ""))

        # ---- Common fields ----
        short_desc   = extract(item.get("short_description", ""))
        description  = extract(item.get("description", ""))
        state        = extract(item.get("state", ""))
        category     = extract(item.get("category", ""))
        priority     = extract(item.get("priority", ""))
        created_on   = extract(item.get("sys_created_on", ""))
        assignment_grp = extract(item.get("assignment_group", ""))

        content = f"""# {table_name.upper()} — {record_number}

## Summary
- **Record Number** : {record_number}
- **Short Description** : {short_desc}
- **State** : {state}
- **Priority** : {priority}
- **Urgency** : {urgency}
- **Impact** : {impact}
- **Category** : {category}
- **Subcategory** : {subcategory}
- **Assignment Group** : {assignment_grp}
- **Assigned To** : {assigned_to}
- **Caller / Requester** : {caller}
- **CI / Asset** : {cmdb_ci}

## Timeline
- **Created On** : {created_on}
- **Opened At** : {opened_at}
- **Resolved At** : {resolved_at}
- **Closed At** : {closed_at}

## Description
{description}

## Close Notes
{close_notes}

## Close Code
{close_code}

## Work Notes / Comments
{work_notes}
"""

        file_path = f"{table_dir}/{record_number}.md"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        written += 1

    print(f"  Written {written} files → knowledge/{table_name}/")

# ============================================
# Main Sync Execution
# ============================================

def main():
    print(f"\nServiceNow Enterprise Sync Starting...")
    print(f"Instance : {BASE_URL}")
    print(f"Fetching records updated/created since: {SINCE_DATE}\n")

    for table, config in TABLES.items():
        try:
            fetch_table_data(table, config)
        except Exception as e:
            print(f"  ERROR syncing {table}: {e}")

    print("\nServiceNow enterprise sync completed successfully.")

if __name__ == "__main__":
    main()
