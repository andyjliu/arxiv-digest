# arxiv-digest

A hands-off daily arXiv screen for cs.AI, cs.CL, cs.GT and cs.LG.

- `.github/workflows/fetch.yml` runs Monday–Friday. It calls `fetch.py`, which pulls the day's announcement from the arXiv RSS feed (new papers, cross-lists and replacements, with abstracts) and commits the result as `data/<date>.jsonl`.
- A scheduled Claude task then follows `INSTRUCTIONS.md`. It screens every paper against the Notion page "arXiv Digest — Projects" and writes the matches to the Notion database "arXiv Daily Picks".

To change what counts as relevant, edit the Notion Projects page. To teach the screen what it gets wrong, set a pick's Status to "Not relevant".
