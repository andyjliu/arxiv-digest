# Slop check (prototype)

Not wired into the daily run. It runs on pushes to the `deslop-prototype` branch. Counts appear in the job summary, and paper-level results are in the `deslop-report` artifact, kept for 3 days. Logs and the summary carry no paper IDs because the repo is public, though any signed-in GitHub user can still download the artifact. The workflow never commits anything or touches Notion.

What it checks, for each arXiv ID:

1. **Hard evidence (would drop):**
   - Leftover chatbot text in the abstract, body or bibliography, e.g. "As an AI language model", "Certainly! Here is…", "[insert citation]". Matches inside figures, tables, listings and quotes are reported but don't count, because papers about LLMs quote model output there.
   - References that resolve nowhere. A reference counts as found if its arXiv ID resolves, its DOI resolves in Crossref, or its title matches OpenAlex, Crossref or Semantic Scholar at ≥0.88 similarity. A miss counts only if OpenAlex and Crossref both answered; otherwise the reference is reported as "lookup failed" and left out of the drop rule. A paper is dropped only if ≥3 references fail **and** they are ≥10% of the checked ones. Cited arXiv IDs that don't resolve are reported separately.
2. **Detector (flag only):** `pangram/editlens_roberta-large` scores the abstract and up to 8 body chunks of ~300 words, spread across the paper. The median body score is used, so a single quoted paragraph can't trigger a flag. The paper is flagged as heavy AI language editing at ≥0.60, or as likely fully AI-generated at ≥0.90.

Known limits:

- **Uncalibrated thresholds.** The detector thresholds are placeholders. Pangram reports a 9.9% false-positive rate for the RoBERTa model on non-native English writing (5.5% for the 3B model). Chunk errors within one paper are correlated, so the median doesn't remove this.
- **HTML only.** Papers without an arXiv HTML version are reported as unchecked. Appendices are skipped, since they are often prompt dumps.
- **Unreliable title extraction.** It assumes the second LaTeXML `bibblock` is the title. Books, blogs and software references often fail lookup without being fake, which is why the drop rule needs several failures.
- **License.** EditLens weights are CC BY-NC-SA 4.0 (non-commercial). Pangram says not to use them to enforce AI-use policies.

Run locally with `pip install -r deslop/requirements.txt`, then `python deslop/check.py --ids-file deslop/picks/2026-10-28.txt`. Tests: `python -m unittest discover -s deslop/tests`.
