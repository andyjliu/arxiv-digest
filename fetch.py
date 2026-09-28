#!/usr/bin/env python3
"""Fetch one day's arXiv announcement for a set of categories from the arXiv RSS feed.

Writes data/<announce-date>.jsonl (one paper per line) and updates data/LATEST.
Stdlib only. Exits 0 without writing if the feed is empty (weekends / holidays).

Usage: python fetch.py [--cats cs.AI+cs.CL+cs.GT+cs.LG] [--from-file feed.xml]
"""
import argparse, email.utils, json, pathlib, re, sys, time, urllib.request
import xml.etree.ElementTree as ET

NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}
UA = "arxiv-digest/1.0 (personal daily digest; mailto:andyliu@cs.cmu.edu)"


def download(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print(f"attempt {i+1} failed: {e}", file=sys.stderr)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cats", default="cs.AI+cs.CL+cs.GT+cs.LG")
    ap.add_argument("--from-file")
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    raw = pathlib.Path(a.from_file).read_bytes() if a.from_file else download(f"https://rss.arxiv.org/rss/{a.cats}")
    day, papers = parse(raw)
    if not papers:
        print("feed empty (no announcement today); nothing written")
        return
    out = pathlib.Path(a.out)
    out.mkdir(exist_ok=True)
    path = out / f"{day}.jsonl"
    with path.open("w") as f:
        for p in sorted(papers, key=lambda p: p["id"]):
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    (out / "LATEST").write_text(day + "\n")
    counts = {}
    for p in papers:
        counts[p["announce_type"]] = counts.get(p["announce_type"], 0) + 1
    print(f"wrote {path}: {len(papers)} papers {counts}")


if __name__ == "__main__":
    main()
