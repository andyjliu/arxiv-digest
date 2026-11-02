"""Offline tests: parsing, leftover detection, decisions. No network."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import check  # noqa: E402
import notion  # noqa: E402

LONG = " ".join(["word"] * 45)

HTML = f"""<html><body><div class="ltx_page_main"><article class="ltx_document">
<div class="ltx_abstract"><h6>Abstract</h6><p class="ltx_p">We study agents. {LONG}</p></div>
<section class="ltx_section"><div class="ltx_para"><p class="ltx_p">Body one. {LONG}</p></div>
<div class="ltx_para"><p class="ltx_p">Certainly! Here is the revised version of the related work. {LONG}</p></div>
<figure class="ltx_figure"><div class="ltx_para"><p class="ltx_p">Prompt: As an AI language model, answer. {LONG}</p></div></figure>
</section>
<section class="ltx_appendix"><div class="ltx_para"><p class="ltx_p">Appendix prompt dump. {LONG}</p></div></section>
<section class="ltx_bibliography"><ul>
<li class="ltx_bibitem"><span class="ltx_bibblock">A. Author and B. Author.</span>
<span class="ltx_bibblock">Persona vectors: Monitoring and controlling character traits in language models.</span>
<span class="ltx_bibblock">arXiv preprint arXiv:2507.21509, 2025.</span></li>
<li class="ltx_bibitem"><span class="ltx_bibblock">C. Author.</span>
<span class="ltx_bibblock">[insert citation here]</span></li>
<li class="ltx_bibitem"><span class="ltx_bibblock">D. Author.</span>
<span class="ltx_bibblock">Some journal paper.</span>
<span class="ltx_bibblock"><a href="https://doi.org/10.1038/nature14539">doi:10.1038/nature14539</a>.</span></li>
</ul></section></article></div></body></html>"""


class T(unittest.TestCase):
    def setUp(self):
        self.p = check.parse_paper(HTML)

    def test_parse(self):
        self.assertTrue(self.p["abstract"].startswith("We study agents"))
        self.assertEqual(len(self.p["refs"]), 3)
        bodies = " ".join(self.p["paragraphs"])
        self.assertIn("Body one", bodies)
        self.assertNotIn("Appendix prompt dump", bodies)
        self.assertNotIn("As an AI language model", bodies)  # figure text is quoted, not body
        self.assertEqual(sum("Body one" in x for x in self.p["paragraphs"]), 1)  # no ltx_para/ltx_p dupes

    def test_leftovers(self):
        hits = check.find_leftovers(self.p)
        wheres = {(h["where"], h["match"].lower()) for h in hits}
        self.assertIn(("body", "certainly! here is"), wheres)
        self.assertIn(("references", "[insert citation here]"), wheres)
        self.assertIn(("figure/table/quote", "as an ai language model"), wheres)

    def test_ids_and_titles(self):
        refs = self.p["refs"]
        self.assertEqual(check.ARXIV_ID_RE.search(refs[0]["text"]).group(1), "2507.21509")
        blob = refs[2]["text"] + " " + " ".join(refs[2]["links"])
        self.assertEqual(check.DOI_RE.search(blob).group(1), "10.1038/nature14539")
        self.assertTrue(check.ref_title(refs[0]).lower().startswith("persona vectors"))

    def test_quoted_only_leftover_does_not_drop(self):
        res = {"leftovers": [{"where": "figure/table/quote", "match": "as an ai language model"}], "refs": []}
        self.assertEqual(check.decide(res)[0], "keep")

    def test_refs_threshold(self):
        refs = [{"status": "found"}] * 27 + [{"status": "not found"}] * 3
        self.assertEqual(check.decide({"leftovers": [], "refs": refs})[0], "drop")
        refs = [{"status": "found"}] * 47 + [{"status": "not found"}] * 3  # 6% < 10%
        self.assertEqual(check.decide({"leftovers": [], "refs": refs})[0], "keep")

    def test_detector_flags(self):
        base = {"leftovers": [], "refs": []}
        f = lambda m: check.decide({**base, "detector": {"body_scores": [m], "body_median": m}})
        self.assertEqual(f(0.95)[0], "flag")
        self.assertIn("fully AI", f(0.95)[1][0])
        self.assertIn("heavy AI language", f(0.7)[1][0])
        self.assertEqual(f(0.3)[0], "keep")

    def test_sim(self):
        self.assertGreater(check.sim("Persona Vectors: Monitoring and Controlling Character Traits",
                                     "Persona vectors: monitoring and controlling character traits"), 0.99)
        self.assertLess(check.sim("Persona vectors", "Attention is all you need"), 0.5)

    def test_abstract_not_in_body(self):
        self.assertNotIn("We study agents", " ".join(self.p["paragraphs"]))

    def test_math_source_stripped(self):
        html = HTML.replace("Body one.", '<math alttext="\\tau"><semantics><mi>τ</mi>'
                                         '<annotation encoding="application/x-tex">\\tau</annotation>'
                                         "</semantics></math> Body one.")
        bodies = " ".join(check.parse_paper(html)["paragraphs"])
        self.assertIn("τ", bodies)
        self.assertNotIn("\\tau", bodies)

    def test_title_keeps_words_starting_like_venues(self):
        for title in ("Instant personalized adaptation via hypernetworks",
                      "The Common Knowledge Effect: Information Sharing and Group Judgment"):
            self.assertEqual(check.ref_title({"blocks": ["A. Author.", title + " ."], "text": ""}), title)

    def test_title_strips_venue_and_prefers_tag(self):
        ref = {"blocks": ["A. Author.", "A real title here. In Proceedings of ICML"], "text": ""}
        self.assertEqual(check.ref_title(ref), "A real title here")
        self.assertEqual(check.ref_title({**ref, "title": "Tagged title."}), "Tagged title")

    def test_title_quoted_across_line_break(self):
        html = HTML.replace("A. Author and B. Author.</span>", "“Quoted title that spans\nacross lines,” J. Foo.</span>")
        self.assertEqual(check.ref_title(check.parse_paper(html)["refs"][0]), "Quoted title that spans across lines")

    def test_no_refs_is_unchecked(self):
        action, reasons = check.decide({"leftovers": [], "refs": [], "n_refs": 0})
        self.assertEqual(action, "unchecked")
        self.assertIn("reference check not run", reasons[0])

    def test_lookup_failure_is_not_counted(self):
        refs = [{"status": "found"}] * 5 + [{"status": "lookup failed"}] * 20
        action, reasons = check.decide({"leftovers": [], "refs": refs})
        self.assertEqual(action, "keep")
        self.assertIn("20 reference lookups failed", reasons[0])

    def test_api_error_gives_lookup_failed(self):
        orig, check.get = check.get, lambda *a, **k: None
        try:
            out = check.check_refs([{"text": "A. Author. A plausible paper title here", "links": [],
                                     "blocks": ["A. Author.", "A plausible paper title here"]}])
        finally:
            check.get = orig
        self.assertEqual(out[0]["status"], "lookup failed")

    def test_miss_on_non_paper_is_not_evidence(self):
        def fake_get(url, *a, **k):
            class R:
                status_code = 200
                json = staticmethod(lambda: {"results": [], "message": {"items": []}, "data": []})
            return R()
        refs = [{"text": t, "links": [], "blocks": ["A.", "A plausible title of four words"]}
                for t in ("Vendor. A plausible title of four words. Company blog, 2026.",
                          "A. Author. A plausible title of four words. Journal of Things 3(2), pp. 1-9.")]
        orig_get, orig_sleep = check.get, check.time.sleep
        check.get, check.time.sleep = fake_get, lambda s: None
        try:
            out = check.check_refs(refs)
        finally:
            check.get, check.time.sleep = orig_get, orig_sleep
        self.assertEqual([o["status"] for o in out], ["not a paper", "not found"])

    def test_sim_ignores_subtitle(self):
        self.assertGreaterEqual(check.sim("Language agents as optimizable graphs",
                                          "GPTSwarm: Language agents as optimizable graphs"), check.TITLE_MATCH)

    def test_summary_has_no_paper_ids(self):
        res = [{"id": "2610.99999", "action": "drop", "refs": [{"status": "not found"}]}]
        text = check.summary(res)
        self.assertNotIn("2610.99999", text)
        self.assertIn("'drop': 1", text)

    def test_chunks(self):
        p = {"abstract": LONG + " more " * 10, "paragraphs": [LONG] * 40}
        ch = check.chunks_for(p)
        self.assertEqual(ch[0][0], "abstract")
        self.assertLessEqual(len([c for c in ch if c[0] == "body"]), check.MAX_CHUNKS)

    def test_notes(self):
        res = {"reasons": ["4/40 references not found"], "detector": {"body_median": 0.034, "body_max": 0.2},
               "refs": [{"status": "not found", "title": f"Missing paper {i}"} for i in range(4)]}
        text = check.notes(res)
        self.assertIn("detector body median 0.03 (max 0.20)", text)
        self.assertIn("not found: Missing paper 2", text)
        self.assertNotIn("Missing paper 3", text)
        self.assertIn("and 1 more", text)
        self.assertEqual(check.notes({"reasons": [], "refs": []}), "no issues found")


class N(unittest.TestCase):
    def test_filter_retries_unchecked_within_lookback(self):
        import datetime
        f = notion.pending_filter(datetime.date(2026, 11, 2))
        self.assertEqual(f["and"][0]["date"]["on_or_after"], "2026-10-26")
        self.assertIn({"property": "Check", "select": {"is_empty": True}}, f["and"][1]["or"])
        self.assertIn({"property": "Check", "select": {"equals": "Unchecked"}}, f["and"][1]["or"])

    def test_row(self):
        page = {"id": "p1", "properties": {"arXiv ID": {"rich_text": [{"plain_text": " 2610.36435 "}]}}}
        self.assertEqual(notion.row(page), ("p1", "2610.36435"))
        self.assertIsNone(notion.row({"id": "p2", "properties": {"arXiv ID": {"rich_text": []}}}))

    def test_properties_touch_only_check_fields(self):
        props = notion.properties("keep", "x" * 3000)
        self.assertEqual(set(props), {"Check", "Check notes"})
        self.assertEqual(props["Check"]["select"]["name"], "OK")
        self.assertEqual(len(props["Check notes"]["rich_text"][0]["text"]["content"]), notion.NOTES_MAX)
        self.assertEqual(set(notion.CHECK.values()), {"OK", "Flag", "Drop", "Unchecked"})  # the Notion options


if __name__ == "__main__":
    unittest.main()
