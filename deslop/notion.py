"""Read unchecked picks from the Notion "arXiv Daily Picks" database and write results back.

Needs NOTION_TOKEN, an internal integration shared only with that database. Only the
Check and Check notes properties are written; Status and everything else are left alone.
Nothing here prints paper IDs or titles, because Actions logs are public.
"""
import datetime
import os

import requests

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"  # data source endpoints
DATA_SOURCE = "91e51fc9-9573-43d6-b6e8-5b343734f2f2"
LOOKBACK_DAYS = 7  # "Unchecked" rows are retried until they are this old (arXiv HTML can lag)
CHECK = {"keep": "OK", "flag": "Flag", "drop": "Drop", "unchecked": "Unchecked"}
NOTES_MAX = 2000  # Notion's limit for one rich_text item


def _headers():
    return {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}", "Notion-Version": VERSION,
            "Content-Type": "application/json"}


def pending_filter(today=None):
    since = (today or datetime.date.today()) - datetime.timedelta(days=LOOKBACK_DAYS)
    return {"and": [
        {"property": "Announced", "date": {"on_or_after": since.isoformat()}},
        {"or": [{"property": "Check", "select": {"is_empty": True}},
                {"property": "Check", "select": {"equals": CHECK["unchecked"]}}]},
    ]}


def row(page):
    """(page id, arXiv ID) for a query result, or None if the row has no arXiv ID."""
    aid = "".join(t.get("plain_text", "") for t in page["properties"].get("arXiv ID", {}).get("rich_text", []))
    return (page["id"], aid.strip()) if aid.strip() else None


def pending():
    """Rows announced in the last LOOKBACK_DAYS whose Check is empty or Unchecked."""
    rows, cursor = [], None
    while True:
        body = {"filter": pending_filter(), "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"{API}/data_sources/{DATA_SOURCE}/query", headers=_headers(), json=body, timeout=30)
        r.raise_for_status()
        d = r.json()
        rows += [x for x in map(row, d["results"]) if x]
        if not d.get("has_more"):
            return rows
        cursor = d["next_cursor"]


def properties(action, notes):
    return {"Check": {"select": {"name": CHECK[action]}},
            "Check notes": {"rich_text": [{"type": "text", "text": {"content": notes[:NOTES_MAX]}}]}}


def write(rows, results, notes_for):
    """Write each result to every row with that arXiv ID. Returns the number of failed updates."""
    by_id = {r["id"]: r for r in results}
    failed = 0
    for page_id, aid in rows:
        res = by_id.get(aid)
        if not res:
            continue
        r = requests.patch(f"{API}/pages/{page_id}", headers=_headers(),
                           json={"properties": properties(res["action"], notes_for(res))}, timeout=30)
        if not r.ok:
            failed += 1
            print(f"Notion update failed: HTTP {r.status_code}")
    return failed
