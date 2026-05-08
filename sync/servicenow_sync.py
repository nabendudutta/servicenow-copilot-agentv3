import os
import requests
from requests.auth import HTTPBasicAuth

# ============================================
# Environment Variables
# ============================================

SNOW_INSTANCE = os.getenv("SNOW_INSTANCE")
SNOW_USER = os.getenv("SNOW_USER")
SNOW_PASSWORD = os.getenv("SNOW_PASSWORD")

# ============================================
# Normalize URL
# ============================================

if SNOW_INSTANCE.startswith("https://"):
    BASE_URL = SNOW_INSTANCE
else:
    BASE_URL = f"https://{SNOW_INSTANCE}.service-now.com"

# ============================================
# ServiceNow Tables to Sync
# ============================================

TABLES = {

    "kb_knowledge": {
        "query": "workflow_state=published",
        "limit": 200
    },

    "incident": {
        "query": "active=false",
        "limit": 500
    },

    "problem": {
        "query": "",
        "limit": 200
    },

    "change_request": {
        "query": "",
        "limit": 200
    },

    #"sc_cat_item": {
     #   "query": "active=true",
      #  "limit": 200
    #},

    "sc_req_item": {
        "query": "",
        "limit": 200
    },

    "sc_task": {
        "query": "",
        "limit": 200
    }
}

# ============================================
# Common Headers
# ============================================

HEADERS = {
    "Accept": "application/json"
}

AUTH = HTTPBasicAuth(
    SNOW_USER,
    SNOW_PASSWORD
)

# ============================================
# Create Knowledge Directory
# ============================================

os.makedirs("knowledge", exist_ok=True)

# ============================================
# Fetch Records
# ============================================

def fetch_table_data(table_name, config):

    print(f"Syncing table: {table_name}")

    url = f"{BASE_URL}/api/now/table/{table_name}"

    params = {
        "sysparm_query": config["query"],
        "sysparm_limit": config["limit"]
    }

    response = requests.get(
        url,
        auth=AUTH,
        headers=HEADERS,
        params=params,
        timeout=120
    )

    response.raise_for_status()

    results = response.json().get("result", [])

    print(f"Fetched {len(results)} records from {table_name}")

    table_dir = f"knowledge/{table_name}"

    os.makedirs(table_dir, exist_ok=True)

    for item in results:

        record_number = (
            item.get("number")
            or item.get("name")
            or item.get("sys_id")
        )

        short_description = item.get(
            "short_description",
            ""
        )

        description = item.get(
            "description",
            ""
        )

        state = item.get(
            "state",
            ""
        )

        category = item.get(
            "category",
            ""
        )

        priority = item.get(
            "priority",
            ""
        )

        content = f'''
Table: {table_name}

Record: {record_number}

Short Description:
{short_description}

Description:
{description}

Category:
{category}

Priority:
{priority}

State:
{state}

Full JSON:
{item}
'''

        file_path = (
            f"{table_dir}/{record_number}.md"
        )

        with open(
            file_path,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(content)

# ============================================
# Main Sync Execution
# ============================================

def main():

    for table, config in TABLES.items():

        try:

            fetch_table_data(
                table,
                config
            )

        except Exception as e:

            print(
                f"Failed syncing {table}: {e}"
            )

    print("ServiceNow enterprise sync completed")


if __name__ == "__main__":
    main()
