#!/usr/bin/env python3
"""Prototype slop check for arXiv picks (not wired into the daily run).

Tier 1 (hard evidence, would DROP):
  - leftover chatbot text in the paper body or bibliography
  - references that resolve in none of arXiv / OpenAlex / Crossref / Semantic Scholar
Tier 2 (FLAG only):
  - EditLens (pangram/editlens_roberta-large) score over the abstract and body chunks,
    flagging both "likely fully AI-generated" and "heavy AI language editing"

Usage:
  python deslop/check.py --ids 2610.36435 2610.37481 ... --out deslop/out
  python deslop/check.py --ids-file deslop/picks/2026-10-28.txt --out deslop/out [--no-detector]

Writes <out>/report.md and <out>/results.json. Nothing is written to Notion.
"""
import argparse
import json
import os
import pathlib
import re
import statistics
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "arxiv-digest-deslop/0.1 (mailto:andyliu@cs.cmu.edu)"}
MAILTO = "andyliu@cs.cmu.edu"
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY")  # optional; without one, a tiny budget shared per IP
S2_KEY = os.environ.get("S2_API_KEY")  # optional
CACHE = {}  # normalized title -> [source, title, score], for titles confirmed found

# ---------------------------------------------------------------- thresholds
# Tier 1. A reference "fails" only if every lookup misses. Unresolvable refs are
# common for books, blogs, tech reports and very new preprints, so drop needs several.
DROP_MIN_FAILED_REFS = 3
DROP_MIN_FAILED_FRAC = 0.10
TITLE_MATCH = 0.88  # normalized title similarity needed to count as found
# Fabricated citations imitate papers. Vendor posts, regulations, repos and books are
# absent from these databases anyway, so a miss only counts for a paper-like reference.
PAPER_LIKE_RE = re.compile(r"\bpp?\.\s*\d|\d+\s*\(\d+\)|\bvol\.|\barxiv\b|\bproceedings\b|\bproc\.|\bconference\b"
                           r"|\bworkshop\b|\bsymposium\b|\bjournal\b|\btransactions\b|\breview\b|\bletters\b", re.I)
# Tier 2. EditLens score = expected bucket / (n_buckets - 1), 0 = human, 1 = AI.
# Placeholders until calibrated on Andy's labels. Use the median over chunks so
# one quoted prompt or boilerplate paragraph can't flag a paper alone.
FULL_AI = 0.90
HEAVY_EDIT = 0.60
MAX_CHUNKS = 8
CHUNK_WORDS = 300  # ~400-450 RoBERTa tokens, under the 512 limit

# Leftover chatbot text. Each pattern is meant to be nearly impossible in a
# human-checked paper. Matches inside quoted examples/prompts are reported but the
# paper is only auto-dropped on matches outside figures/tables/code/quotes.
LEFTOVER_PATTERNS = [
    r"\bas an ai (language )?model\b",
    r"\bas a large language model\b",
    r"\bi (?:cannot|can't|am unable to) (?:browse|access) the internet\b",
    r"\bcertainly[!,] here(?:'s| is| are)\b",
    r"\bsure[!,] here(?:'s| is| are)\b",
    r"\bhere is (?:a|the) (?:revised|rewritten|polished|improved) (?:version|paragraph|abstract)\b",
    r"\bi hope this helps\b",
    r"\blet me know if you(?:'d| would)? (?:like|want|need)\b",
    r"\bregenerate response\b",
    r"\bmy knowledge cutoff\b",
    r"\[insert [^\]]{1,40}\]",
    r"\[citation needed\]",
    r"\[(?:add|cite) (?:a )?(?:reference|citation)s?[^\]]{0,30}\]",
    r"\bplaceholder (?:text|citation|reference)\b",
]
LEFTOVER_RE = re.compile("|".join(LEFTOVER_PATTERNS), re.I)

ARXIV_ID_RE = re.compile(r"(?:arxiv[:\s./]*|abs/|pdf/)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+[^\s\"<>,;.)\]])", re.I)


def get(url, params=None, tries=3, sleep=1.0, headers=None):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=30)
            if r.status_code == 429:
                time.sleep(sleep * (2 ** i) * 3)
                continue
            return r
        except requests.RequestException:
            time.sleep(sleep * (2 ** i))
    return None


def norm_title(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.95 if min(len(a), len(b)) > 25 else 0.0  # same title, with or without a subtitle
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- paper parsing
def fetch_html(arxiv_id):
    """Return (html, error). Distinguishes 'no HTML version' from network failure."""
    r = get(f"https://arxiv.org/html/{arxiv_id}")
    if r is None:
        return None, "could not reach arxiv.org; not checked"
    if r.status_code == 404 or (r.status_code == 200 and "ltx_page_main" not in r.text):
        return None, "no arXiv HTML version (LaTeX conversion missing); not checked"
    if r.status_code != 200:
        return None, f"arxiv.org returned HTTP {r.status_code}; not checked"
    return r.text, None


def squash(el):
    return " ".join(el.get_text(" ", strip=True).split())


def parse_paper(html):
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("annotation, annotation-xml"):
        el.decompose()  # LaTeX source of each formula; would repeat every math expression as TeX
    abstract = soup.select_one(".ltx_abstract")
    abstract_text = squash(abstract) if abstract else ""
    abstract_text = re.sub(r"^\s*abstract\s*", "", abstract_text, flags=re.I)
    for el in soup.select(".ltx_abstract, .ltx_acknowledgements"):
        el.decompose()

    bib = soup.select_one(".ltx_bibliography")
    refs = []
    if bib:
        for li in bib.select("li.ltx_bibitem"):
            blocks = [squash(b) for b in li.select(".ltx_bibblock")]
            title = li.select_one(".ltx_bib_title")
            links = [a.get("href", "") for a in li.select("a[href]")]
            refs.append({"text": squash(li), "blocks": blocks, "links": links,
                         "title": squash(title) if title else ""})
        bib.decompose()

    # Separate "quoted" regions (figures, tables, listings, verbatim, quotes) where
    # prompts and model outputs are legitimately shown.
    quoted = []
    for el in soup.select("figure, table, .ltx_listing, .ltx_verbatim, blockquote, .ltx_quote, pre, code"):
        quoted.append(el.get_text(" ", strip=True))
        el.decompose()
    for el in soup.select(".ltx_appendix"):
        el.decompose()  # appendices are often prompt dumps; keep body only for tier 2 and leftovers
    paras = []
    for p in soup.select(".ltx_para, .ltx_p"):
        t = squash(p)
        if len(t.split()) >= 40 and t not in paras:
            paras.append(t)
    # .ltx_p is nested inside .ltx_para; drop paragraphs contained in an earlier one
    dedup = []
    for t in paras:
        if not any(t in d for d in dedup):
            dedup.append(t)
    return {"abstract": abstract_text, "paragraphs": dedup, "quoted": quoted, "refs": refs}


# ---------------------------------------------------------------- tier 1
def find_leftovers(paper):
    hits = []
    for where, texts in (("body", paper["paragraphs"]), ("abstract", [paper["abstract"]]),
                         ("references", [r["text"] for r in paper["refs"]]),
                         ("figure/table/quote", paper["quoted"])):
        for t in texts:
            for m in LEFTOVER_RE.finditer(t):
                s = max(0, m.start() - 80)
                hits.append({"where": where, "match": m.group(0), "context": t[s:m.end() + 80]})
    return hits


def ref_title(ref):
    """Title from a LaTeXML bibitem: tagged if BibTeX-derived, else usually the 2nd bibblock."""
    if ref.get("title"):
        return ref["title"].strip(" .,")
    b = ref["blocks"]
    cand = b[1] if len(b) >= 2 else ref["text"]
    m = re.search(r"[\"“](.{15,250}?)[\"”]", ref["text"])
    if m:
        cand = m.group(1)
    # Venue after the title. Needs the preceding punctuation, or "Instant..." and "Inference" match.
    cand = re.sub(r"[.,]\s+(In|Proceedings|arXiv preprint|Advances in)\b.*$", "", cand)
    return cand.strip(" .,")


def check_arxiv_ids(ids):
    """Return (ids that resolve, whether every batch got an answer)."""
    found, complete = set(), True
    ids = sorted(set(ids))
    for i in range(0, len(ids), 50):
        r = get("http://export.arxiv.org/api/query",
                {"id_list": ",".join(ids[i:i + 50]), "max_results": 50})
        if r is None or r.status_code != 200:
            complete = False
            continue
        root = ET.fromstring(r.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            eid = (e.findtext("a:id", "", ns) or "").rsplit("/", 1)[-1]
            title = e.findtext("a:title", "", ns) or ""
            if title.strip().lower() != "error":
                found.add(re.sub(r"v\d+$", "", eid))
        time.sleep(3)  # arXiv API etiquette
    return found, complete


def check_doi(doi):
    r = get(f"https://api.crossref.org/works/{doi}", {"mailto": MAILTO})
    return r is not None and r.status_code == 200


def search_title(title):
    """Return (source, best_title, score, answered).

    answered is False if OpenAlex or Crossref gave no answer, in which case a miss
    means nothing. Semantic Scholar is a bonus: it often rate-limits without a key.
    """
    key = norm_title(title)
    if key in CACHE:
        return (*CACHE[key], True)
    best = ("", "", 0.0)
    answered = True
    params = {"search": title[:300], "per-page": 5, "mailto": MAILTO}
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    r = get("https://api.openalex.org/works", params)
    if r is not None and r.status_code == 200:
        for w in r.json().get("results", []):
            s = sim(title, w.get("title") or "")
            if s > best[2]:
                best = ("openalex", w.get("title"), s)
    else:
        answered = False
    if best[2] < TITLE_MATCH:
        r = get("https://api.crossref.org/works", {"query.bibliographic": title[:300], "rows": 5, "mailto": MAILTO})
        if r is not None and r.status_code == 200:
            for w in r.json().get("message", {}).get("items", []):
                t = (w.get("title") or [""])[0]
                s = sim(title, t)
                if s > best[2]:
                    best = ("crossref", t, s)
        else:
            answered = False
    if best[2] < TITLE_MATCH:
        time.sleep(1.1)  # unauthenticated S2 is ~1 req/s shared
        r = get("https://api.semanticscholar.org/graph/v1/paper/search/match", {"query": title[:300]},
                tries=3 if S2_KEY else 1, headers={"x-api-key": S2_KEY} if S2_KEY else None)
        if r is not None and r.status_code == 200:
            for w in r.json().get("data", []):
                s = sim(title, w.get("title") or "")
                if s > best[2]:
                    best = ("semanticscholar", w.get("title"), s)
    if best[2] >= TITLE_MATCH:
        CACHE[key] = list(best)
    return (*best, answered)


def check_refs(refs):
    out = []
    want_arxiv = {}
    for i, ref in enumerate(refs):
        blob = ref["text"] + " " + " ".join(ref["links"])
        m = ARXIV_ID_RE.search(blob)
        d = DOI_RE.search(blob)
        out.append({"i": i, "text": ref["text"][:300], "title": ref_title(ref),
                    "arxiv": m.group(1) if m else None, "doi": d.group(1) if d else None,
                    "status": None, "how": None})
        if m:
            want_arxiv[i] = m.group(1)
    found_ids, ids_complete = check_arxiv_ids(want_arxiv.values()) if want_arxiv else (set(), True)
    for o in out:
        if o["arxiv"]:
            if o["arxiv"] in found_ids:
                o["status"], o["how"] = "found", "arxiv id"
                continue
            o["bad_arxiv_id"] = ids_complete  # cited ID doesn't resolve; still try title
        if o["doi"] and check_doi(o["doi"]):
            o["status"], o["how"] = "found", "doi"
            continue
        if len(norm_title(o["title"]).split()) < 4:
            o["status"], o["how"] = "skipped", "no usable title"
            continue
        src, t, s, answered = search_title(o["title"])
        o["best_match"], o["best_score"] = t, round(s, 3)
        if s >= TITLE_MATCH:
            o["status"], o["how"] = "found", src
        elif answered and not PAPER_LIKE_RE.search(o["text"]):
            o["status"], o["how"] = "not a paper", src or "none"
        elif answered:
            o["status"], o["how"] = "not found", src or "none"
        else:  # an API failure is not evidence against the reference
            o["status"], o["how"] = "lookup failed", src or "none"
    return out


# ---------------------------------------------------------------- tier 2
class Detector:
    def __init__(self, ckpt="pangram/editlens_roberta-large", base="FacebookAI/roberta-large"):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(base)
        self.model = AutoModelForSequenceClassification.from_pretrained(ckpt).eval()
        self.n = self.model.config.num_labels

    @staticmethod
    def clean(t):  # mirrors EditLens preprocess.clean_text (lowercase, whitespace)
        return re.sub(r"\s+", " ", t.lower()).strip()

    def score(self, texts):
        torch = self.torch
        enc = self.tok([self.clean(t) for t in texts], truncation=True, max_length=512,
                       padding=True, return_tensors="pt")
        with torch.no_grad():
            p = torch.softmax(self.model(**enc).logits, dim=-1)
        return (p @ torch.arange(self.n, dtype=p.dtype) / (self.n - 1)).tolist()


def chunks_for(paper):
    out = [("abstract", paper["abstract"])] if len(paper["abstract"].split()) >= 50 else []
    buf, n = [], 0
    for p in paper["paragraphs"]:
        buf.append(p)
        n += len(p.split())
        if n >= CHUNK_WORDS:
            out.append(("body", " ".join(" ".join(buf).split()[:CHUNK_WORDS + 60])))
            buf, n = [], 0
    if n >= 120:
        out.append(("body", " ".join(buf)))
    body = [c for c in out if c[0] == "body"]
    if len(body) > MAX_CHUNKS:  # spread evenly over the paper
        step = len(body) / MAX_CHUNKS
        body = [body[int(i * step)] for i in range(MAX_CHUNKS)]
    return [c for c in out if c[0] == "abstract"] + body


# ---------------------------------------------------------------- decision
def decide(res):
    reasons, action = [], "keep"
    lo = [h for h in res["leftovers"] if h["where"] != "figure/table/quote"]
    if lo:
        action = "drop"
        reasons.append(f"leftover chatbot text ({len(lo)}x, e.g. '{lo[0]['match']}')")
    checked = [r for r in res["refs"] if r["status"] in ("found", "not found")]
    bad = [r for r in checked if r["status"] == "not found"]
    if checked and len(bad) >= DROP_MIN_FAILED_REFS and len(bad) / len(checked) >= DROP_MIN_FAILED_FRAC:
        action = "drop"
        reasons.append(f"{len(bad)}/{len(checked)} references not found in any database")
    elif bad:
        reasons.append(f"{len(bad)}/{len(checked)} references not found (below drop threshold)")
    failed = [r for r in res["refs"] if r["status"] == "lookup failed"]
    if failed:
        reasons.append(f"{len(failed)} reference lookups failed (API errors); not counted")
    bad_ids = [r for r in res["refs"] if r.get("bad_arxiv_id")]
    if bad_ids:
        reasons.append(f"{len(bad_ids)} cited arXiv IDs don't resolve")
    d = res.get("detector")
    if d and d["body_scores"]:
        med = d["body_median"]
        if med >= FULL_AI:
            reasons.append(f"detector: likely fully AI-generated (median {med:.2f})")
            action = "flag" if action == "keep" else action
        elif med >= HEAVY_EDIT:
            reasons.append(f"detector: heavy AI language editing (median {med:.2f})")
            action = "flag" if action == "keep" else action
    if res.get("error"):
        reasons.append(res["error"])
        action = "unchecked" if action == "keep" else action
    if res.get("n_refs") == 0:  # e.g. an empty References section; not evidence the paper is clean
        reasons.append("no references extracted; reference check not run")
        action = "unchecked" if action == "keep" else action
    return action, reasons


def run(ids, out, use_detector=True, cache_path=None):
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cache = pathlib.Path(cache_path) if cache_path else None
    if cache and cache.exists():
        CACHE.update(json.loads(cache.read_text()))
    det, det_error = None, None
    if use_detector:
        try:
            det = Detector()
        except Exception as e:  # e.g. gated access still pending; tier 1 must still run
            det_error = f"{type(e).__name__}: {str(e)[:200]}"
    results = []
    for n, aid in enumerate(ids):
        if n:
            time.sleep(16)  # arxiv.org robots.txt Crawl-delay: 15
        print(f"== {aid}", file=sys.stderr)
        res = {"id": aid, "leftovers": [], "refs": []}
        html, err = fetch_html(aid)
        if html is None:
            res["error"] = err
        else:
            paper = parse_paper(html)
            res["n_paragraphs"], res["n_refs"] = len(paper["paragraphs"]), len(paper["refs"])
            res["leftovers"] = find_leftovers(paper)
            res["refs"] = check_refs(paper["refs"])
            if det:
                ch = chunks_for(paper)
                sc = det.score([t for _, t in ch]) if ch else []
                body = [s for (k, _), s in zip(ch, sc) if k == "body"]
                absr = [s for (k, _), s in zip(ch, sc) if k == "abstract"]
                res["detector"] = {"abstract": absr[0] if absr else None, "body_scores": body,
                                   "body_median": statistics.median(body) if body else None,
                                   "body_max": max(body) if body else None}
        res["action"], res["reasons"] = decide(res)
        results.append(res)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(CACHE))
    (out / "results.json").write_text(json.dumps(results, indent=1))
    (out / "report.md").write_text(report(results))
    (out / "summary.md").write_text(summary(results, use_detector, det_error))
    print((out / "summary.md").read_text())  # counts only: Actions logs are public


def summary(results, use_detector=True, det_error=None):
    """Counts only, with no paper IDs, for the public job summary."""
    refs = [x for r in results for x in r["refs"]]
    counts = {a: sum(r["action"] == a for r in results) for a in ("keep", "flag", "drop", "unchecked")}
    tally = {s: sum(x["status"] == s for x in refs) for s in ("found", "not found", "not a paper", "lookup failed", "skipped")}
    scored = sum(bool((r.get("detector") or {}).get("body_scores")) for r in results)
    det = "off" if not use_detector else f"failed to load ({det_error})" if det_error else f"scored {scored} papers"
    return "\n".join([f"Papers: {len(results)}. Actions: {counts}.", f"References: {tally}.", f"Detector: {det}.", ""])


def report(results):
    L = ["# Slop check report", "",
         f"Thresholds: drop if leftover chatbot text, or >= {DROP_MIN_FAILED_REFS} refs not found and "
         f">= {DROP_MIN_FAILED_FRAC:.0%} of checked refs. Flag if EditLens body median >= {FULL_AI} "
         f"(fully AI) or >= {HEAVY_EDIT} (heavy AI editing). Detector thresholds are uncalibrated.", "",
         "| arXiv | action | refs found/checked | abstract | body median | body max | reasons |",
         "|---|---|---|---|---|---|---|"]
    for r in results:
        checked = [x for x in r["refs"] if x["status"] in ("found", "not found")]
        found = [x for x in checked if x["status"] == "found"]
        d = r.get("detector") or {}
        f = lambda v: "" if v is None else f"{v:.2f}"
        L.append(f"| {r['id']} | {r['action']} | {len(found)}/{len(checked)} | {f(d.get('abstract'))} | "
                 f"{f(d.get('body_median'))} | {f(d.get('body_max'))} | {'; '.join(r['reasons'])} |")
    L.append("")
    for r in results:
        bad = [x for x in r["refs"] if x["status"] == "not found"]
        if not (bad or r["leftovers"]):
            continue
        L += [f"## {r['id']}", ""]
        for h in r["leftovers"]:
            L.append(f"- leftover text in {h['where']}: `{h['match']}` … {h['context']}")
        for x in bad:
            L.append(f"- not found: {x['title'][:140]} (best: {x.get('best_match') or '-'}, "
                     f"{x.get('best_score', 0)}) — raw: {x['text'][:160]}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--ids-file")
    ap.add_argument("--out", default="deslop/out")
    ap.add_argument("--no-detector", action="store_true")
    ap.add_argument("--cache", help="JSON file of confirmed reference lookups, kept across runs")
    a = ap.parse_args()
    ids = list(a.ids)
    if a.ids_file:
        ids += [l.strip() for l in open(a.ids_file) if l.strip() and not l.startswith("#")]
    run(ids, a.out, use_detector=not a.no_detector, cache_path=a.cache)
