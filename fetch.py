#!/usr/bin/env python3
"""Fetch one day's arXiv announcement for a set of categories from the arXiv RSS feed.

Writes data/<announce-date>.jsonl (one paper per line) and updates data/LATEST.
Stdlib only. Exits 0 without writing if the feed is empty (weekends / holidays).

Usage: python fetch.py [--cats cs.AI+cs.CL+cs.GT+cs.LG] [--from-file feed.xml]
       python fetch.py --date 2026-09-25   # backfill a past announcement via the arXiv API
"""
import argparse, datetime as dt, email.utils, json, pathlib, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}
HEADERS = {
    "User-Agent": "arxiv-digest/1.1 (personal research digest)",
    "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
}


def download(url, tries=4):
    print(f"GET {url}", file=sys.stderr)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:800].decode("utf-8", "replace")
            print(f"attempt {i+1} failed: HTTP {e.code} {e.reason}\n  headers: {dict(e.headers)}\n  body: {body}",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"attempt {i+1} failed: {e!r}", file=sys.stderr)
        time.sleep(10 * (i + 1))
    raise SystemExit("could not download feed")


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    chan = root.find("channel")
    papers, dates = {}, []
    for it in chan.findall("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", link) or re.search(r"(\d{4}\.\d{4,5})(v\d+)?", guid)
        if not m:
            continue
        pid = m.group(1)
        desc = it.findtext("description") or ""
        abstract = desc.split("Abstract:", 1)[1].strip() if "Abstract:" in desc else desc.strip()
        atype = (it.findtext("arxiv:announce_type", namespaces=NS) or "").strip()
        if not atype:
            mm = re.search(r"Announce Type:\s*(\S+)", desc)
            atype = mm.group(1) if mm else ""
        cats = [c.text.strip() for c in it.findall("category") if c.text]
        authors = (it.findtext("dc:creator", namespaces=NS) or "").strip()
        pub = it.findtext("pubDate")
        if pub:
            try:
                dates.append(email.utils.parsedate_to_datetime(pub).date().isoformat())
            except Exception:  # noqa: BLE001
                pass
        papers[pid] = {
            "id": pid,
            "url": f"https://arxiv.org/abs/{pid}",
            "title": re.sub(r"\s+", " ", (it.findtext("title") or "").strip()),
            "authors": authors,
            "categories": cats,
            "announce_type": atype,  # new | cross | replace | replace-cross
            "abstract": re.sub(r"\s+", " ", abstract),
        }
    day = max(set(dates), key=dates.count) if dates else None
    if day is None:
        pub = chan.findtext("pubDate") or chan.findtext("lastBuildDate")
        if pub:
            day = email.utils.parsedate_to_datetime(pub).date().isoformat()
    return day, list(papers.values())


ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom",
        "os": "http://a9.com/-/spec/opensearch/1.1/"}


def prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def backfill(day_str, cats, parse_only=None):
    """Approximate a past announcement via the arXiv API.

    The mailing announced on weekday D contains papers submitted between 14:00 ET on the
    weekday before the previous weekday and 14:00 ET on the previous weekday. Papers are
    matched on their v1 submission date, so replacements are excluded. A paper whose
    primary category is in `cats` is labelled "new"; otherwise it is labelled "cross".
    Papers that arXiv held or delayed are missed.
    """
    et = ZoneInfo("America/New_York")
    day = dt.date.fromisoformat(day_str)
    end_d = prev_weekday(day)
    start_d = prev_weekday(end_d)
    to_utc = lambda d: dt.datetime.combine(d, dt.time(14, 0), et).astimezone(dt.timezone.utc).strftime("%Y%m%d%H%M")
    catq = " OR ".join(f"cat:{c}" for c in cats.split("+"))
    q = f"({catq}) AND submittedDate:[{to_utc(start_d)} TO {to_utc(end_d)}]"
    wanted = set(cats.split("+"))
    papers, start, total = {}, 0, None
    while total is None or start < total:
        # urlencode percent-encodes ( ) [ ] and turns spaces into '+'; raw brackets get a 406 from arXiv's CDN
        params = urllib.parse.urlencode({"search_query": q, "start": start, "max_results": 500,
                                         "sortBy": "submittedDate", "sortOrder": "ascending"}, safe=":")
        url = f"https://export.arxiv.org/api/query?{params}"
        raw = parse_only if parse_only is not None else download(url)
        root = ET.fromstring(raw)
        total = int(root.findtext("os:totalResults", namespaces=ATOM) or 0)
        entries = root.findall("a:entry", ATOM)
        for e in entries:
            m = re.search(r"(\d{4}\.\d{4,5})", e.findtext("a:id", namespaces=ATOM) or "")
            if not m:
                continue
            pid = m.group(1)
            prim = e.find("arxiv:primary_category", ATOM)
            prim = prim.get("term") if prim is not None else ""
            cats_e = [c.get("term") for c in e.findall("a:category", ATOM)]
            papers[pid] = {
                "id": pid,
                "url": f"https://arxiv.org/abs/{pid}",
                "title": re.sub(r"\s+", " ", (e.findtext("a:title", namespaces=ATOM) or "").strip()),
                "authors": ", ".join(a.findtext("a:name", namespaces=ATOM) or "" for a in e.findall("a:author", ATOM)),
                "categories": cats_e,
                "announce_type": "new" if prim in wanted else "cross",
                "abstract": re.sub(r"\s+", " ", (e.findtext("a:summary", namespaces=ATOM) or "").strip()),
            }
        if parse_only is not None or not entries:
            break
        start += len(entries)
        time.sleep(3)  # arXiv API etiquette
    print(f"API: totalResults={total}, parsed={len(papers)}")
    return day_str, list(papers.values())


def write(day, papers, out):
    out = pathlib.Path(out)
    out.mkdir(exist_ok=True)
    path = out / f"{day}.jsonl"
    with path.open("w") as f:
        for p in sorted(papers, key=lambda p: p["id"]):
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    latest = out / "LATEST"
    cur = latest.read_text().strip() if latest.exists() else ""
    if day > cur:
        latest.write_text(day + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cats", default="cs.AI+cs.CL+cs.GT+cs.LG")
    ap.add_argument("--from-file")
    ap.add_argument("--date", help="backfill a past announcement date (YYYY-MM-DD) via the arXiv API")
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    if a.date:
        raw = pathlib.Path(a.from_file).read_bytes() if a.from_file else None
        day, papers = backfill(a.date, a.cats, parse_only=raw)
    else:
        raw = pathlib.Path(a.from_file).read_bytes() if a.from_file else download(f"https://rss.arxiv.org/rss/{a.cats}")
        day, papers = parse(raw)
    if not papers:
        print("no papers (no announcement today?); nothing written")
        return
    path = write(day, papers, a.out)
    counts = {}
    for p in papers:
        counts[p["announce_type"]] = counts.get(p["announce_type"], 0) + 1
    print(f"wrote {path}: {len(papers)} papers {counts}")


if __name__ == "__main__":
    main()
