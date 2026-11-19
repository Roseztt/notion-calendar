#!/usr/bin/env python3
# Builds a calendar of Kaleidoscope events from Notion.
# Uses the official API with a key, or the public pages without one.
#
#     pip install requests
#     python notion_calendar.py                     # make calendar.html (this semester)
#     python notion_calendar.py --all-semesters     # every semester
#     python notion_calendar.py --public            # ignore the key
#     python notion_calendar.py --csv events.csv    # also save a CSV
#     python notion_calendar.py --keep-semesters    # include "Semester" rows
import argparse
import csv
import json
import os
import pathlib
import re
import sys
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

# Calendars to pull from ("date_column" defaults to DATE_COLUMN)
SOURCES = [
    {
        "data_source": "303de33c-c3a2-802f-943f-000b55454255",
        "label": "Kaleidoscope Events",
    },
    {
        "page": "2f0de33c-c3a2-81b9-a8ed-e7c58f0cfef5",
        "label": "Club Events",
        "date_column": "Event Date",
        "status": "Approved",  # only approved requests
    },
]

# Column names in Notion
DATE_COLUMN = "Date"
CURRENT_SEMESTER_COLUMN = "Current Semester"

# Notion API key (keep this file private)
NOTION_API_KEY = ""

API_BASES = ["https://www.notion.so/api/v3", "https://app.notion.com/api/v3"]
TIMEZONE = "America/New_York"
# Property types to skip
SKIP_DETAIL_TYPES = {
    "formula", "rollup", "button", "person", "created_by", "last_edited_by",
    "file", "created_time", "last_edited_time",
}

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (notion-calendar)",
})
_api_base = None
store = {"block": {}, "collection": {}, "collection_view": {}}


# Public pages (no key)

# Call Notion, trying each address
def post(endpoint, payload):
    global _api_base
    bases = [_api_base] if _api_base else API_BASES
    last_err = None
    for base in bases:
        try:
            r = session.post(f"{base}/{endpoint}", json=payload, timeout=30)
            if r.status_code == 200:
                _api_base = base
                return r.json()
            last_err = f"{base}: HTTP {r.status_code} {r.text[:200]}"
        except requests.RequestException as e:
            last_err = f"{base}: {e}"
    raise RuntimeError(f"{endpoint} failed -> {last_err}")


# Unwrap a Notion record
def unwrap(rec):
    if not isinstance(rec, dict):
        return None
    v = rec.get("value", rec)
    if isinstance(v, dict) and "id" not in v and isinstance(v.get("value"), dict):
        v = v["value"]
    return v if isinstance(v, dict) else None


def merge(record_map):
    for table in store:
        for rid, rec in (record_map or {}).get(table, {}).items():
            v = unwrap(rec)
            if v:
                store[table][rid] = v


def load_page(page_id):
    cursor, chunk = {"stack": []}, 0
    while True:
        data = post("loadPageChunk", {
            "pageId": page_id, "limit": 100, "cursor": cursor,
            "chunkNumber": chunk, "verticalColumns": False,
        })
        merge(data.get("recordMap"))
        cursor = data.get("cursor") or {"stack": []}
        if not cursor.get("stack"):
            break
        chunk += 1


def fetch_records(table, ids):
    missing = [i for i in dict.fromkeys(ids) if i and i not in store[table]]
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        data = post("syncRecordValues", {
            "requests": [{"pointer": {"table": table, "id": x}, "version": -1} for x in batch]
        })
        merge(data.get("recordMap"))


# Find databases on a page
def find_databases(root_id, max_depth=3):
    found, seen_blocks, seen_collections = [], set(), set()

    def walk(bid, depth):
        if bid in seen_blocks or depth > max_depth:
            return
        seen_blocks.add(bid)
        if bid not in store["block"]:
            fetch_records("block", [bid])
        b = store["block"].get(bid)
        if not b:
            return
        btype = b.get("type")
        if btype in ("collection_view", "collection_view_page"):
            cid = b.get("collection_id") or (b.get("format") or {}).get(
                "collection_pointer", {}).get("id")
            views = b.get("view_ids") or []
            if cid and views and cid not in seen_collections:
                seen_collections.add(cid)
                found.append((cid, views[0], b.get("space_id")))
            return
        if btype == "page" and bid != root_id:
            load_page(bid)
            depth += 1
        for child in b.get("content") or []:
            walk(child, depth)

    load_page(root_id)
    walk(root_id, 0)
    return found


# Find a database by ID
def lookup_data_source(cid):
    fetch_records("collection", [cid])
    coll = store["collection"].get(cid)
    if not coll:
        return []
    parent = coll.get("parent_id")
    fetch_records("block", [parent])
    b = store["block"].get(parent) or {}
    views = b.get("view_ids") or []
    if not views:
        return []
    return [(cid, views[0], b.get("space_id") or coll.get("space_id"))]


# Get all row IDs
def query_collection(cid, view_id, space_id):
    data = post("queryCollection", {
        "collection": {"id": cid, "spaceId": space_id},
        "collectionView": {"id": view_id, "spaceId": space_id},
        "loader": {
            "type": "reducer",
            "reducers": {"collection_group_results": {"type": "results", "limit": 10000}},
            "searchQuery": "",
            "userTimeZone": TIMEZONE,
        },
    })
    merge(data.get("recordMap"))
    res = (data.get("result", {}).get("reducerResults", {})
           .get("collection_group_results", {}))
    ids = res.get("blockIds", [])
    fetch_records("block", ids)
    fetch_records("collection", [cid])
    return ids


# Notion data to events

def decorations(value):
    for seg in value or []:
        for deco in (seg[1] if len(seg) > 1 else []):
            if deco:
                yield deco


def first_date(value):
    return next((d[1] for d in decorations(value) if d[0] == "d"), None)


def fmt_date(d):
    start = d.get("start_date", "")
    if d.get("start_time"):
        start += " " + d["start_time"]
    end = d.get("end_date") or ""
    if end and d.get("end_time"):
        end += " " + d["end_time"]
    elif not end and d.get("end_time"):
        end = d["end_time"]
    return f"{start} – {end}" if end else start


# Rich text to plain text
def rich_text(value):
    out = []
    for seg in value or []:
        text = seg[0] if seg else ""
        decos = seg[1] if len(seg) > 1 else []
        date = next((d[1] for d in decos if d and d[0] == "d"), None)
        page = next((d[1] for d in decos if d and d[0] == "p" and len(d) > 1), None)
        if date:
            out.append(fmt_date(date))
        elif text == "\u2023" and page:
            b = store["block"].get(page)
            out.append(rich_text((b or {}).get("properties", {}).get("title")) or "linked page")
        elif text == "\u2023":
            continue  # skip @mentions
        else:
            out.append(text)
    return "".join(out)


def pick_date_property(schema, date_column=DATE_COLUMN):
    dates = [pid for pid, m in schema.items() if m.get("type") == "date"]
    for pid in dates:
        if schema[pid].get("name", "").strip().lower() == date_column.lower():
            return pid
    return dates[0] if dates else None


# Row to event (None if no date)
def to_event(block, schema, calendar, date_column=DATE_COLUMN):
    props = block.get("properties") or {}
    date_id = pick_date_property(schema, date_column)
    d = first_date(props.get(date_id)) if date_id else None
    if not d or not d.get("start_date"):
        return None
    details = {}
    for pid, meta in schema.items():
        if pid in ("title", date_id) or meta.get("type") in SKIP_DETAIL_TYPES:
            continue
        text = rich_text(props.get(pid)).strip()
        if text:
            details[meta.get("name", pid)] = text
    return {
        "title": rich_text(props.get("title")).strip() or "Untitled",
        "start": d["start_date"],
        "end": d.get("end_date") or d["start_date"],
        "startTime": d.get("start_time"),
        "endTime": d.get("end_time"),
        "calendar": calendar,
        "url": f"https://www.notion.so/{block['id'].replace('-', '')}",
        "details": details,
    }


def notion_id(text):
    path = text.split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12})$",
                  path, re.I)
    if not m:
        raise ValueError(f"Couldn't find a Notion id in: {text}")
    return str(uuid.UUID(m.group(1).replace("-", "").lower()))


# Semester rows seen while loading (used to find the current semester)
SEMESTER_ROWS = []


# Apply the filters
def keep_event(ev, args):
    if ev["details"].get("Type") == "Semester":
        SEMESTER_ROWS.append(ev)
        return args.keep_semesters
    return True


# Backup: semester from today's date (used without a key)
def current_semester():
    today = date.today().isoformat()
    for s in SEMESTER_ROWS:
        if s["start"] <= today <= s["end"]:
            return {"name": s["title"], "start": s["start"], "end": s["end"]}
    y, m = date.today().year, date.today().month
    if m >= 9:
        return {"name": f"Fall {y}", "start": f"{y}-09-01", "end": f"{y}-12-31"}
    if m <= 4:
        return {"name": f"Spring {y}", "start": f"{y}-01-01", "end": f"{y}-04-30"}
    return {"name": f"Summer {y}", "start": f"{y}-05-01", "end": f"{y}-08-31"}


# Get events from public pages
def collect_source(src, args):
    target = notion_id(src["data_source"]) if src.get("data_source") else None
    page = src.get("page") or src.get("url")
    if page:
        databases = find_databases(notion_id(page))
        if target:
            databases = [d for d in databases if d[0] == target] or lookup_data_source(target)
    else:
        databases = lookup_data_source(target)
    if not databases:
        raise RuntimeError("no databases found (is the page still public?)")

    events, undated = [], 0
    for cid, view_id, space_id in databases:
        ids = query_collection(cid, view_id, space_id)
        coll = store["collection"].get(cid, {})
        calendar = src.get("label") or rich_text(coll.get("name")).strip() or "Notion"
        schema = coll.get("schema") or {}
        rows = [store["block"].get(i) for i in ids]
        rows = [b for b in rows if b and b.get("alive") is not False]
        # Load linked page titles
        fetch_records("block", [d[1] for b in rows
                                for v in (b.get("properties") or {}).values()
                                for d in decorations(v) if d[0] == "p" and len(d) > 1])
        count = 0
        for b in rows:
            ev = to_event(b, schema, calendar, src.get("date_column", DATE_COLUMN))
            if ev is None:
                undated += 1
                continue
            if src.get("status") and ev["details"].get("Status") != src["status"]:
                continue
            if keep_event(ev, args):
                events.append(ev)
                count += 1
        print(f"  {calendar}: {count} events")
    return events, undated


# Official API (with key)

NOTION_VERSION = "2026-03-11"
SKIP_OFFICIAL_TYPES = {
    "title", "relation", "rollup", "files", "button", "unique_id", "verification",
    "created_time", "last_edited_time", "created_by", "last_edited_by",
}


class NotionError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f"Notion API error {status}: {text[:300]}")
        self.status = status


def api_key():
    return (os.environ.get("NOTION_API_KEY") or NOTION_API_KEY).strip()


def api(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    for _ in range(5):
        r = requests.request(method, f"https://api.notion.com/v1/{path}",
                             headers=headers, json=body, timeout=30)
        if r.status_code == 429:  # rate limited, retry
            time.sleep(float(r.headers.get("Retry-After", 1)))
            continue
        if not r.ok:
            raise NotionError(r.status_code, r.text)
        return r.json()
    raise NotionError(429, "still rate limited after 5 tries")


# Find databases to read
def official_data_sources(src):
    if src.get("data_source"):
        return [notion_id(src["data_source"])]
    nid = notion_id(src.get("page") or src["url"])
    try:
        return [ds["id"] for ds in api("GET", f"databases/{nid}").get("data_sources", [])]
    except NotionError as e:
        if e.status not in (400, 404):
            raise
    found, cursor = [], None
    while True:
        path = f"blocks/{nid}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = api("GET", path)
        for b in data["results"]:
            if b.get("type") == "child_database":
                found += [ds["id"] for ds in
                          api("GET", f"databases/{b['id']}").get("data_sources", [])]
        if not data.get("has_more"):
            return found
        cursor = data["next_cursor"]


# Split into (date, time)
def split_dt(value):
    if not value:
        return None, None
    if "T" not in value:
        return value[:10], None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo:
        try:
            dt = dt.astimezone(ZoneInfo(TIMEZONE))
        except Exception:  # fall back to local time
            dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def official_date_text(d):
    sd, st = split_dt(d.get("start"))
    ed, et = split_dt(d.get("end"))
    start = f"{sd} {st}" if st else (sd or "")
    end = f"{ed} {et}" if et else (ed or "")
    return f"{start} – {end}" if end else start


# Property to text
def plain(prop):
    t = prop.get("type")
    v = prop.get(t)
    if v is None:
        return ""
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v)
    if t in ("select", "status"):
        return v.get("name", "")
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in v)
    if t == "people":
        return ", ".join(x.get("name", "") for x in v if x.get("name"))
    if t == "checkbox":
        return "Yes" if v else "No"
    if t == "date":
        return official_date_text(v)
    if t == "formula":
        ft = v.get("type")
        fv = v.get(ft)
        if ft == "boolean":
            return "Yes" if fv else "No"
        if ft == "date":
            return official_date_text(fv) if fv else ""
        return "" if fv is None else str(fv)
    if t in ("number", "url", "email", "phone_number"):
        return str(v)
    return ""


def official_event(page, calendar, date_column=DATE_COLUMN):
    props = page.get("properties", {})
    dates = [n for n, p in props.items() if p.get("type") == "date"]
    date_name = next((n for n in dates if n.strip().lower() == date_column.lower()),
                     dates[0] if dates else None)
    d = props[date_name].get("date") if date_name else None
    if not d or not d.get("start"):
        return None
    sd, st = split_dt(d["start"])
    ed, et = split_dt(d.get("end"))
    title = next((plain(p) for p in props.values() if p.get("type") == "title"), "")
    details = {}
    for name, p in props.items():
        if name == date_name or p.get("type") in SKIP_OFFICIAL_TYPES:
            continue
        text = plain(p).strip()
        if text:
            details[name] = text
    return {
        "title": title.strip() or "Untitled",
        "start": sd,
        "end": ed or sd,
        "startTime": st,
        "endTime": et,
        "calendar": calendar,
        "url": page.get("url", ""),
        "details": details,
    }


# Find the Semester row with Current Semester checked
def find_current_semester():
    query = {"page_size": 10, "filter": {"and": [
        {"property": CURRENT_SEMESTER_COLUMN, "formula": {"checkbox": {"equals": True}}},
        {"property": "Type", "select": {"equals": "Semester"}},
    ]}}
    for src in SOURCES:
        try:
            for ds_id in official_data_sources(src):
                data = api("POST", f"data_sources/{ds_id}/query", query)
                for page in data.get("results", []):
                    ev = official_event(page, "")
                    if ev:
                        return {"name": ev["title"], "start": ev["start"], "end": ev["end"]}
        except NotionError:
            continue  # this database doesn't have those columns
    return None


# Get events from the API
def collect_official(src, args, semester=None):
    ds_ids = official_data_sources(src)
    if not ds_ids:
        raise RuntimeError("no databases found on this page")
    events, undated = [], 0
    for ds_id in ds_ids:
        info = api("GET", f"data_sources/{ds_id}")
        calendar = src.get("label") or "".join(
            t.get("plain_text", "") for t in info.get("title", [])).strip() or "Notion"
        date_col = src.get("date_column", DATE_COLUMN)
        filters = []
        if semester:  # only events in the semester
            filters += [
                {"property": date_col, "date": {"on_or_after": semester["start"]}},
                {"property": date_col, "date": {"on_or_before": semester["end"]}},
            ]
        if src.get("status"):
            filters.append({"property": "Status", "status": {"equals": src["status"]}})
        count, cursor = 0, None
        while True:
            body = {"page_size": 100}
            if filters:
                body["filter"] = {"and": filters}
            if cursor:
                body["start_cursor"] = cursor
            try:
                data = api("POST", f"data_sources/{ds_id}/query", body)
            except NotionError as e:
                if e.status == 400 and filters and not cursor:
                    filters = []  # column missing, filter afterwards instead
                    continue
                raise
            for page in data.get("results", []):
                if page.get("object") != "page":
                    continue
                ev = official_event(page, calendar, date_col)
                if ev is None:
                    undated += 1
                elif src.get("status") and ev["details"].get("Status") != src["status"]:
                    continue
                elif keep_event(ev, args):
                    events.append(ev)
                    count += 1
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
            time.sleep(0.35)  # rate limit
        print(f"  {calendar}: {count} events")
    return events, undated


# Output

def write_csv(events, path):
    extra = []
    for e in events:
        for k in e["details"]:
            if k not in extra:
                extra.append(k)
    cols = ["Calendar", "Title", "Start date", "Start time", "End date", "End time",
            *extra, "Notion URL"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for e in events:
            w.writerow([e["calendar"], e["title"], e["start"], e["startTime"] or "",
                        e["end"], e["endTime"] or "",
                        *[e["details"].get(k, "") for k in extra], e["url"]])


def build_html(events, meta):
    def safe(obj):  # safe for <script>
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    shown = ("title", "start", "end", "startTime", "endTime", "calendar")
    slim = [{k: e[k] for k in shown} for e in events]
    return HTML.replace("__EVENTS__", safe(slim)).replace("__META__", safe(meta))


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kaleidoscope events</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #F3F5F8; --surface: #FFFFFF; --ink: #1A1E27; --muted: #5C6474;
  --line: #DCE1E9; --off: #ECEFF3;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #13151A; --surface: #1B1E25; --ink: #E7EAF0; --muted: #9AA2B1;
    --line: #2B303A; --off: #171920;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.45 "Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 48px; }
header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
h1 { margin: 0; font-size: clamp(2.1rem, 6vw, 3.6rem); line-height: 1; letter-spacing: -0.025em; }
h1 .yr { font-weight: 400; color: var(--muted); }
.controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
button { font: inherit; color: inherit; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 7px 12px; cursor: pointer; }
button:hover { border-color: var(--muted); }
button:disabled { opacity: .35; cursor: default; border-color: var(--line); }
button:focus-visible, a:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.src { display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px 5px 9px; border-radius: 999px; }
.sw { width: 12px; height: 12px; border-radius: 3px; background: var(--c); flex: none; }
.src .n { color: var(--muted); }
.src[aria-pressed="false"] { color: var(--muted); }
.src[aria-pressed="false"] .sw { background: transparent; box-shadow: inset 0 0 0 2px var(--c); }

.grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); background: var(--surface); border-top: 1px solid var(--line); border-left: 1px solid var(--line); }
.dow { padding: 8px 10px; font-size: .85rem; color: var(--muted); border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.day { min-height: 118px; min-width: 0; padding: 6px 6px 8px; display: flex; flex-direction: column; gap: 3px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.day.out { background: var(--off); }
.day.out .num { color: var(--muted); }
.num { align-self: flex-start; font-size: .9rem; padding: 1px 6px; border-radius: 6px; }
.day.today .num { background: var(--ink); color: var(--bg); font-weight: 700; }
.ev { display: block; width: 100%; text-align: left; border: 0; border-left: 3px solid var(--c); border-radius: 5px; padding: 3px 6px; font-size: .8rem; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: color-mix(in srgb, var(--c) 15%, var(--surface)); }
.ev:hover { background: color-mix(in srgb, var(--c) 26%, var(--surface)); }
.ev .t { color: var(--muted); margin-right: 5px; }
.more, .link { border: 0; background: none; padding: 2px 6px; color: var(--muted); text-align: left; text-decoration: underline; text-underline-offset: 2px; }
.more { font-size: .8rem; }
.note { color: var(--muted); margin: 14px 0 0; }
.note .link { padding: 0; }

footer { margin-top: 18px; color: var(--muted); font-size: .85rem; }

dialog { padding: 0; border: 1px solid var(--line); border-radius: 12px; width: min(520px, 92vw); background: var(--surface); color: var(--ink); }
dialog::backdrop { background: rgb(0 0 0 / .4); }
#dlgBody { padding: 20px 22px 6px; border-top: 6px solid var(--c, var(--ink)); }
#dlgBody h2 { margin: 6px 0 4px; font-size: 1.45rem; line-height: 1.2; }
.dsrc { margin: 0; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: .9rem; }
.when { margin: 0 0 16px; }
#dlgBody .ev { font-size: .95rem; padding: 7px 10px; margin-bottom: 6px; white-space: normal; }
.dlgfoot { display: flex; justify-content: flex-end; padding: 0 22px 18px; margin: 0; }

@media (max-width: 700px) {
  .wrap { padding: 20px 12px 40px; }
  .day { min-height: 70px; padding: 4px 3px; gap: 2px; }
  .dow { padding: 6px 4px; font-size: .75rem; }
  .num { font-size: .8rem; padding: 0 4px; }
  .ev { font-size: .68rem; padding: 2px 3px; border-left-width: 2px; }
  .ev .t { display: none; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title"></h1>
    <div class="controls">
      <div id="nav" class="controls">
        <button id="today">Today</button>
        <button id="prev" aria-label="Previous month">&lsaquo;</button>
        <button id="next" aria-label="Next month">&rsaquo;</button>
      </div>
    </div>
  </header>
  <div id="legend" class="legend" role="group" aria-label="Calendars"></div>
  <section id="month">
    <div id="grid" class="grid"></div>
    <p id="note" class="note"></p>
  </section>
  <footer id="foot"></footer>
</div>
<dialog id="dlg">
  <div id="dlgBody"></div>
  <form method="dialog" class="dlgfoot"><button>Close</button></form>
</dialog>
<script>
const EVENTS = __EVENTS__;
const META = __META__;
const PALETTE = ["#0E7C86", "#6A4CD6", "#C2410C", "#2F7D32", "#B4236B", "#8A6D0B"];
const calendars = [...new Set(EVENTS.map(e => e.calendar))];
const color = Object.fromEntries(calendars.map((c, i) => [c, PALETTE[i % PALETTE.length]]));
const hidden = new Set();

const $ = id => document.getElementById(id);
const pad = n => String(n).padStart(2, "0");
const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const todayKey = ymd(new Date());
const byTime = (a, b) => (a.startTime || "").localeCompare(b.startTime || "") || a.title.localeCompare(b.title);
const visible = e => !hidden.has(e.calendar);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function fmtTime(t) {
  if (!t) return "";
  const [h, m] = t.split(":").map(Number);
  const h12 = h % 12 || 12, ap = h < 12 ? "AM" : "PM";
  return m ? `${h12}:${pad(m)} ${ap}` : `${h12} ${ap}`;
}

function fmtDay(s, withYear) {
  const opts = { weekday: "short", month: "short", day: "numeric" };
  if (withYear) opts.year = "numeric";
  return parse(s).toLocaleDateString(undefined, opts);
}

function timeRange(e) {
  if (e.end !== e.start) {
    const s = fmtTime(e.startTime), en = fmtTime(e.endTime);
    return `${fmtDay(e.start)}${s ? " " + s : ""} – ${fmtDay(e.end)}${en ? " " + en : ""}`;
  }
  if (!e.startTime) return "";
  return e.endTime ? `${fmtTime(e.startTime)} – ${fmtTime(e.endTime)}` : fmtTime(e.startTime);
}

function when(e) {
  if (e.end !== e.start) return timeRange(e);
  const t = timeRange(e);
  return t ? `${fmtDay(e.start, true)}, ${t}` : fmtDay(e.start, true);
}

function dayIndex() {
  const idx = {};
  for (const e of EVENTS.filter(visible)) {
    const d = parse(e.start), end = parse(e.end || e.start);
    for (let i = 0; d <= end && i < 400; i++) {
      (idx[ymd(d)] ||= []).push(e);
      d.setDate(d.getDate() + 1);
    }
  }
  Object.values(idx).forEach(list => list.sort(byTime));
  return idx;
}

// Keep the calendar inside the semester
const SEM = META.semester;
const monthOf = s => { const [y, m] = s.split("-").map(Number); return new Date(y, m - 1, 1); };
const minMonth = SEM ? monthOf(SEM.start) : null;
const maxMonth = SEM ? monthOf(SEM.end) : null;
function clampMonth(m) {
  if (minMonth && m < minMonth) return minMonth;
  if (maxMonth && m > maxMonth) return maxMonth;
  return m;
}
function thisMonth() { const n = new Date(); return clampMonth(new Date(n.getFullYear(), n.getMonth(), 1)); }
let cur = thisMonth();

function step(n) { cur = clampMonth(new Date(cur.getFullYear(), cur.getMonth() + n, 1)); render(); }

function swatch(c) { const s = el("span", "sw"); s.style.setProperty("--c", color[c]); return s; }

function renderLegend() {
  const box = $("legend");
  box.replaceChildren();
  for (const c of calendars) {
    const on = !hidden.has(c);
    const b = el("button", "src");
    b.style.setProperty("--c", color[c]);
    b.setAttribute("aria-pressed", String(on));
    b.title = on ? `Hide ${c}` : `Show ${c}`;
    b.append(el("span", "sw"), el("span", null, c),
             el("span", "n", String(EVENTS.filter(e => e.calendar === c).length)));
    b.onclick = () => { on ? hidden.add(c) : hidden.delete(c); render(); };
    box.append(b);
  }
}

function chip(e) {
  const b = el("button", "ev");
  b.style.setProperty("--c", color[e.calendar]);
  if (e.startTime) b.append(el("span", "t", fmtTime(e.startTime)));
  b.append(document.createTextNode(e.title));
  b.title = `${e.title}, ${when(e)}`;
  b.onclick = () => openEvent(e);
  return b;
}

function renderMonth() {
  const idx = dayIndex();
  $("title").replaceChildren(cur.toLocaleDateString(undefined, { month: "long" }) + " ",
                             el("span", "yr", String(cur.getFullYear())));
  const grid = $("grid");
  grid.replaceChildren();
  for (let i = 0; i < 7; i++) {
    grid.append(el("div", "dow", new Date(2023, 0, 1 + i).toLocaleDateString(undefined, { weekday: "short" })));
  }
  const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
  const days = new Date(cur.getFullYear(), cur.getMonth() + 1, 0).getDate();
  const cells = Math.ceil((first.getDay() + days) / 7) * 7;
  const d = new Date(first);
  d.setDate(1 - first.getDay());
  let inMonth = 0;
  for (let i = 0; i < cells; i++) {
    const key = ymd(d), evs = idx[key] || [];
    const cell = el("div", "day");
    cell.setAttribute("aria-label", d.toLocaleDateString(undefined, { dateStyle: "full" }));
    if (d.getMonth() !== cur.getMonth()) cell.classList.add("out");
    else inMonth += evs.length;
    if (key === todayKey) cell.classList.add("today");
    cell.append(el("span", "num", String(d.getDate())));
    const limit = evs.length > 3 ? 2 : 3;
    evs.slice(0, limit).forEach(e => cell.append(chip(e)));
    if (evs.length > limit) {
      const more = el("button", "more", `${evs.length - limit} more`);
      more.onclick = () => openDay(key, evs);
      cell.append(more);
    }
    grid.append(cell);
    d.setDate(d.getDate() + 1);
  }

  const note = $("note");
  note.replaceChildren();
  if (!EVENTS.length) {
    note.textContent = "No events came back from Notion. Check that both pages are still public, then run the script again.";
  } else if (!inMonth) {
    const monthEnd = ymd(new Date(cur.getFullYear(), cur.getMonth() + 1, 0));
    const next = EVENTS.filter(visible).map(e => e.start).filter(s => s > monthEnd).sort()[0];
    note.append("No events this month. ");
    if (next) {
      const b = el("button", "link", `Go to the next one (${fmtDay(next, true)})`);
      b.onclick = () => { const [y, m] = next.split("-").map(Number); cur = new Date(y, m - 1, 1); render(); };
      note.append(b);
    }
  }
}

function showDialog() { const d = $("dlg"); if (!d.open) d.showModal(); }

function openEvent(e) {
  const box = $("dlgBody");
  box.replaceChildren();
  box.style.setProperty("--c", color[e.calendar]);
  const src = el("p", "dsrc");
  src.append(swatch(e.calendar), e.calendar);
  box.append(src, el("h2", null, e.title), el("p", "when", when(e)));
  showDialog();
}

function openDay(key, evs) {
  const box = $("dlgBody");
  box.replaceChildren();
  box.style.removeProperty("--c");
  box.append(el("h2", null, parse(key).toLocaleDateString(undefined, { dateStyle: "full" })));
  evs.forEach(e => box.append(chip(e)));
  showDialog();
}

function render() {
  renderLegend();
  renderMonth();
  $("prev").disabled = Boolean(minMonth && cur <= minMonth);
  $("next").disabled = Boolean(maxMonth && cur >= maxMonth);
}

$("prev").onclick = () => step(-1);
$("next").onclick = () => step(1);
$("today").onclick = () => { cur = thisMonth(); render(); };
$("dlg").addEventListener("click", ev => { if (ev.target === $("dlg")) $("dlg").close(); });
document.addEventListener("keydown", ev => {
  if ($("dlg").open || ev.target.closest("input, textarea")) return;
  if (ev.key === "ArrowLeft") step(-1);
  if (ev.key === "ArrowRight") step(1);
});

$("foot").textContent = (SEM ? `${SEM.name}. ` : "") +
  `Pulled from Notion on ${META.generated}. ${EVENTS.length} events` +
  (META.undated ? `; ${META.undated} rows without a date aren’t shown.` : ".");
render();
</script>
</body>
</html>
"""


# Name key that ignores club order
def event_name_key(title):
    title = " ".join(title.split()).casefold()
    pieces = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    names, rest = pieces[0], (pieces[1] if len(pieces) > 1 else "")
    parts = sorted(n.strip() for n in re.split(r",|&|\band\b", names) if n.strip())
    return (tuple(parts), rest)


def main():
    ap = argparse.ArgumentParser(description="Show events from public Notion pages on one calendar")
    ap.add_argument("-o", "--output", default="calendar.html", help="calendar file to write")
    ap.add_argument("--csv", help="also save every event to this CSV file")
    ap.add_argument("--keep-semesters", action="store_true",
                    help='keep rows whose Type is "Semester" (skipped by default)')
    ap.add_argument("--all-semesters", action="store_true",
                    help="show every semester, not just the current one")
    ap.add_argument("--public", action="store_true",
                    help="ignore the API key and read the public pages instead")
    args = ap.parse_args()

    official = bool(api_key()) and not args.public
    print("Using the official Notion API" if official else "Using the public pages (no key)")

    # Current semester from Notion (needs the key)
    semester = None
    if official and not args.all_semesters:
        semester = find_current_semester()
        if semester:
            print(f"Current semester in Notion: {semester['name']}")
        else:
            print("Couldn't find a Semester row with Current Semester checked", file=sys.stderr)

    events, undated = [], 0
    for src in SOURCES:
        print("Loading " + (f"data source {src['data_source']}" if src.get("data_source")
                            else f"page {src.get('page') or src.get('url')}"))
        try:
            evs, nd = collect_official(src, args, semester) if official else collect_source(src, args)
        except NotionError as e:
            hint = {401: "the key was rejected; check it or create a new one",
                    403: "the key doesn't have permission to read this",
                    404: "the key can't see this page; if it's a connection, add it to the "
                         "page via ••• > Add connections"}.get(e.status, "")
            print(f"  Skipped this page: {e}" + (f"\n  Hint: {hint}" if hint else ""),
                  file=sys.stderr)
            continue
        except Exception as e:  # skip failed calendars
            print(f"  Skipped this page: {e}", file=sys.stderr)
            continue
        events += evs
        undated += nd

    # Remove duplicates (same day and name), keep the earliest
    events.sort(key=lambda e: (e["start"], e["startTime"] or ""))
    seen, unique = set(), []
    for e in events:
        key = (event_name_key(e["title"]), e["start"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    if len(unique) < len(events):
        print(f"Removed {len(events) - len(unique)} duplicate events")
    unique.sort(key=lambda e: (e["start"], e["startTime"] or "", e["title"]))

    # Only this semester (falls back to dates if Notion didn't say)
    if not args.all_semesters:
        semester = semester or current_semester()
        unique = [e for e in unique
                  if e["end"] >= semester["start"] and e["start"] <= semester["end"]]
        print(f"Showing {semester['name']} ({semester['start']} to {semester['end']})")

    now = datetime.now()
    meta = {"generated": f"{now:%B} {now.day}, {now.year} at {now:%H:%M}",
            "undated": undated, "semester": semester}
    out = pathlib.Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(unique, meta), encoding="utf-8")
    print(f"Wrote {len(unique)} events to {out}")
    if not unique:
        print("No events found. Check that the pages are still public.", file=sys.stderr)

    if args.csv:
        write_csv(unique, args.csv)
        print(f"Saved CSV to {pathlib.Path(args.csv).resolve()}")
    print(f"\nOpen your calendar: {out.as_uri()}")


if __name__ == "__main__":
    main()
