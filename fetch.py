#!/usr/bin/env python3
"""Fetch one day's arXiv announcement for a set of categories from the arXiv RSS feed.

Writes data/<announce-date>.jsonl (one paper per line) and updates data/LATEST.
Stdlib only. Exits 0 without writing if the feed is empty (weekends / holidays).

Usage: python fetch.py [--cats cs.AI+cs.CL+cs.GT+cs.LG] [--from-file feed.xml]
       python fetch.py --listing [--expect 2026-09-25]   # latest announcement from arxiv.org/list/<cat>/new
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


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def _text(html):
    html = re.sub(r"<[^>]+>", " ", html)
    for k, v in {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}.items():
        html = html.replace(k, v)
    return re.sub(r"\s+([,;:.])", r"\1", re.sub(r"\s+", " ", html)).strip()


def parse_listing(html, wanted):
    """Parse an arxiv.org/list/<cat>/new page into (announce_date, papers)."""
    day = None
    m = re.search(r"(?:listings|submissions) for \w+,?\s+(\d{1,2})\s+(\w+)\s+(\d{2,4})", html, re.I)
    if m:
        mon = MONTHS.get(m.group(2).lower()) or next((v for k, v in MONTHS.items() if k.startswith(m.group(2).lower()[:3])), None)
        yr = int(m.group(3)); yr += 2000 if yr < 100 else 0
        if mon:
            day = dt.date(yr, mon, int(m.group(1))).isoformat()
    # section boundaries (New submissions / Cross-lists / Replacements)
    marks = sorted((mm.start(), kind) for kind, pat in
                   [("new", r"<h3[^>]*>\s*New submissions"), ("cross", r"<h3[^>]*>\s*Cross"), ("replace", r"<h3[^>]*>\s*Replacement")]
                   for mm in re.finditer(pat, html, re.I))
    papers = {}
    for dtm in re.finditer(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", html, re.S):
        idm = re.search(r"arXiv:(\d{4}\.\d{4,5})", dtm.group(1))
        if not idm:
            continue
        dd = dtm.group(2)
        section = "new"
        for pos, kind in marks:
            if pos < dtm.start():
                section = kind
        title = re.search(r"list-title[^>]*>(.*?)</div>", dd, re.S)
        authors = re.search(r"list-authors[^>]*>(.*?)</div>", dd, re.S)
        subjects = re.search(r"list-subjects[^>]*>(.*?)</div>", dd, re.S)
        primary = re.search(r"primary-subject[^>]*>(.*?)</span>", dd, re.S)
        abstract = re.search(r"<p class=['\"]mathjax['\"]>(.*?)</p>", dd, re.S)
        cats = re.findall(r"\(([a-z\-]+(?:\.[A-Za-z\-]+)?)\)", _text(subjects.group(1))) if subjects else []
        prim = re.findall(r"\(([a-z\-]+(?:\.[A-Za-z\-]+)?)\)", _text(primary.group(1))) if primary else cats[:1]
        if section != "replace":
            section = "new" if (prim and prim[0] in wanted) else "cross"
        pid = idm.group(1)
        prev = papers.get(pid)
        if prev and prev["announce_type"] == "new":
            continue
        papers[pid] = {
            "id": pid,
            "url": f"https://arxiv.org/abs/{pid}",
            "title": re.sub(r"^Title:\s*", "", _text(title.group(1))) if title else "",
            "authors": re.sub(r"^Authors?:\s*", "", _text(authors.group(1))) if authors else "",
            "categories": cats,
            "announce_type": section,
            "abstract": _text(abstract.group(1)) if abstract else "",
        }
    return day, papers


def fetch_listing(cats, expect=None, save_raw=None):
    """Most recent announcement from the arxiv.org/list/<cat>/new pages (allowed by robots.txt,
    15 s crawl delay). Useful on weekends and as a same-day fallback when the RSS feed fails.
    It only ever covers the latest announcement; older days cannot be recovered this way."""
    wanted = set(cats.split("+"))
    papers, days = {}, set()
    for i, cat in enumerate(cats.split("+")):
        if i:
            time.sleep(16)
        html = download(f"https://arxiv.org/list/{cat}/new?skip=0&show=2000").decode("utf-8", "replace")
        if save_raw:
            pathlib.Path(save_raw).mkdir(parents=True, exist_ok=True)
            (pathlib.Path(save_raw) / f"{cat}.html").write_text(html[:200000])
        day, ps = parse_listing(html, wanted)
        print(f"{cat}: announce date {day}, {len(ps)} entries")
        days.add(day)
        for pid, p in ps.items():
            if pid not in papers or (p["announce_type"] == "new" and papers[pid]["announce_type"] != "new"):
                papers[pid] = p
    days.discard(None)
    if len(days) != 1:
        raise SystemExit(f"could not determine a single announce date from listings: {days}")
    day = days.pop()
    if expect and expect != day:
        raise SystemExit(f"requested {expect} but the latest listing is {day}; older days cannot be recovered")
    return day, list(papers.values())


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
    ap.add_argument("--listing", action="store_true", help="read the latest announcement from arxiv.org/list/<cat>/new instead of RSS")
    ap.add_argument("--expect", help="with --listing: fail unless the latest announcement is this date (YYYY-MM-DD)")
    ap.add_argument("--save-raw", help="with --listing: save raw HTML here for debugging")
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    if a.listing and a.from_file:
        day, ps = parse_listing(pathlib.Path(a.from_file).read_text(), set(a.cats.split("+")))
        papers = list(ps.values())
    elif a.listing:
        day, papers = fetch_listing(a.cats, a.expect, a.save_raw)
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
