#!/usr/bin/env python3
"""Prepare a day's papers for screening: drop replacements, split into batch files.

Usage: python prep.py data/2026-09-28.jsonl --outdir /tmp/batches --size 100
Prints a summary and the list of batch files.
"""
import argparse, json, pathlib

ap = argparse.ArgumentParser()
ap.add_argument("path")
ap.add_argument("--outdir", default="/tmp/batches")
ap.add_argument("--size", type=int, default=100)
ap.add_argument("--include-replacements", action="store_true")
a = ap.parse_args()

rows = [json.loads(l) for l in open(a.path)]
keep = [r for r in rows if a.include_replacements or not r["announce_type"].startswith("replace")]
out = pathlib.Path(a.outdir)
out.mkdir(parents=True, exist_ok=True)
for old in out.glob("batch_*.txt"):
    old.unlink()
files = []
for i in range(0, len(keep), a.size):
    f = out / f"batch_{i // a.size:02d}.txt"
    with f.open("w") as fh:
        for r in keep[i : i + a.size]:
            fh.write(f"[{r['id']}] {r['title']}\nCategories: {', '.join(r['categories'])}\n{r['abstract']}\n\n")
    files.append(str(f))
print(f"{len(rows)} total, {len(keep)} after dropping replacements, {len(files)} batches")
print("\n".join(files))
