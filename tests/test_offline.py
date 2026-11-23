"""Offline tests for lit-download: no network, no browser, no real settings.

    python -X utf8 -m unittest discover -s tests -v        (run from the skill folder)

Everything runs against a throw-away LITDL_HOME, so the user's own ~/.litdl is never read or written.
What is covered: the holdings judgment (which database for which paper), WebVPN address rewriting, the school
settings, the ledger, portal-page recognition, the guards of request-batch, and that every command written in
the docs exists in the scripts. What is not: anything that needs a site to answer.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="litdl-test-"))
os.environ["LITDL_HOME"] = str(TMP / "home")
os.environ["LITDL_WEBVPN"] = "webvpn.example.edu.cn"
os.environ["LITDL_EBSCO_OPID"] = "opid1"
(TMP / "home").mkdir()
sys.path.insert(0, str(SCRIPTS))
import litdl  # noqa: E402
import litbrowser  # noqa: E402

VPN = "webvpn.example.edu.cn"


def row(bucket, **kw):
    r = {"id": 1, "bucket": bucket, "doi": "10.1000/x", "title": "A Title About Things", "status": "todo", "tried": {}}
    r.update(kw)
    return r


def run(script, *args, home=None, env=None):
    e = {**os.environ, "LITDL_HOME": str(home or TMP / "home"), "PYTHONIOENCODING": "utf-8", **(env or {})}
    p = subprocess.run([sys.executable, "-X", "utf8", "-P", str(SCRIPTS / script), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=e)
    return p.returncode, p.stdout + p.stderr


def use_holdings(obj=None, vpn=VPN):
    """Point litdl at a holdings.json in the test home (None: the shipped starting table only)."""
    f = TMP / "home" / "holdings.json"
    if obj is None:
        f.unlink(missing_ok=True)
    else:
        f.write_text(json.dumps(obj), encoding="utf-8")
    litdl._HOLD = None
    litdl.WEBVPN_DEFAULT = vpn


class Sources(unittest.TestCase):
    def setUp(self):
        use_holdings()

    def test_school_database_first_then_scihub_then_public(self):
        page, pdf = "https://x/page", "https://x/pdf"
        cases = [
            (row("informs"), ["ebsco", "scihub", "public"]),
            (row("wiley", webvpn_page=page), ["wiley", "ebsco", "scihub", "public"]),
            (row("springer", webvpn_page=page, webvpn_pdf=pdf), ["direct", "scihub", "public"]),
            (row("springer"), ["direct", "scihub", "public"]),            # addresses are made now, not read from the plan
            (row("elsevier", webvpn_page=page, pii="S0925527315000237"), ["elsevier", "scihub", "public"]),
            (row("elsevier", webvpn_page=page), ["scihub", "public"]),
            (row("tandf", webvpn_page=page), ["tandf", "ebsco", "scihub", "public"]),
            (row("emerald"), ["pubdoi", "ebsco", "scihub", "public"]),
            (row("acm"), ["scihub", "public"]),
            (row("other"), ["ebsco", "scihub", "public"]),
        ]
        for r, want in cases:
            self.assertEqual(litdl.sources(r), want, r["bucket"])

    def test_open_access_publisher_goes_to_its_own_page_first(self):
        self.assertEqual(litdl.sources(row("nature", is_oa=True)), ["public", "scihub"])
        self.assertEqual(litdl.sources(row("nature")), ["scihub", "public"])
        # a repository copy of a subscription paper must not jump ahead of the school's database
        self.assertEqual(litdl.sources(row("informs", is_oa=True, oa_pdf="https://repo/x.pdf"))[0], "ebsco")

    def test_rows_that_have_no_database_here(self):
        for b in ("arxiv", "cnki", "unknown"):
            self.assertEqual(litdl.sources(row(b)), [])
        self.assertEqual(litdl.required(row("unknown")), [])

    def test_no_school_webvpn(self):
        use_holdings(vpn="none")
        self.assertEqual(litdl.sources(row("informs")), ["scihub", "public"])
        self.assertEqual(litdl.sources(row("tandf")), ["tandf", "scihub", "public"])

    def test_the_users_own_table_overrides(self):
        use_holdings({"bucket": {"tandf": ["vpnpub", "tandf", "ebsco"], "emerald": []}, "scihub": False})
        self.assertEqual(litdl.sources(row("tandf", webvpn_page="https://x/p")), ["vpnpub", "tandf", "ebsco", "public"])
        self.assertEqual(litdl.sources(row("tandf")), ["vpnpub", "tandf", "ebsco", "public"])   # address made now
        litdl.WEBVPN_DEFAULT = "none"
        self.assertEqual(litdl.sources(row("tandf")), ["tandf", "public"])                   # no WebVPN: none of those
        litdl.WEBVPN_DEFAULT = VPN
        self.assertEqual(litdl.sources(row("emerald")), ["public"])
        self.assertEqual(litdl.sources(row("informs")), ["ebsco", "public"])               # untouched buckets stay

    def test_vpnpub_is_never_used_for_publishers_with_a_recipe_of_their_own(self):
        use_holdings({"bucket": {"wiley": ["vpnpub", "wiley"]}})
        self.assertEqual(litdl.sources(row("wiley", webvpn_page="https://x/p"))[0], "wiley")

    def test_ledger_decides_what_is_next_and_what_counts_as_exhausted(self):
        r = row("tandf")
        self.assertEqual(litdl.required(r), ["publisher", "ebsco", "scihub", "public"])
        self.assertEqual(litdl.next_source(r), "tandf")
        r["tried"] = {"publisher": "x"}
        self.assertEqual(litdl.next_source(r), "ebsco")
        self.assertTrue(litdl.route_text(r).split(" → ")[0].endswith("✗"))        # tried, no copy
        r["tried"] = {"publisher": "已下到（2026-01-01）"}
        self.assertTrue(litdl.route_text(r).split(" → ")[0].endswith("✓"))        # this database gave the file
        r["tried"] = {c: "x" for c in litdl.required(r)}
        self.assertIsNone(litdl.next_source(r))
        self.assertEqual(litdl.required(row("springer", webvpn_pdf="p")), ["publisher", "scihub", "public"])

    def test_every_recipe_has_a_ledger_name_and_a_label(self):
        for name in litbrowser.RECIPES:
            self.assertIn(name, litdl.LEDGER)
            self.assertIn(name, litdl.SOURCE_CN)
            self.assertIn(name, litbrowser.GAPS)
            self.assertIn(name, litbrowser.FETCH_TIMEOUT)
        self.assertTrue(set(litdl.LEDGER.values()) <= set(litdl.CHANNELS))


class Addresses(unittest.TestCase):
    def setUp(self):
        use_holdings()

    def test_usual_rule(self):
        self.assertEqual(litdl.vpn_host("www.sciencedirect.com", VPN), f"https://www-sciencedirect-com-443.{VPN}")

    def test_a_school_that_writes_one_site_differently(self):
        use_holdings({"webvpn_hosts": {"link.springer.com": "link-springer-com.{vpn}:443",
                                       "www.tandfonline.com": "www-tandfonline-com.{vpn}"}})
        self.assertEqual(litdl.vpn_host("link.springer.com", VPN), f"https://link-springer-com.{VPN}:443")
        page, pdf = litdl.webvpn_links(row("springer", doi="10.1007/s1"), VPN)
        self.assertEqual(page, f"https://link-springer-com.{VPN}:443/article/10.1007/s1")
        self.assertTrue(pdf.endswith("/content/pdf/10.1007/s1.pdf"))
        self.assertEqual(litdl.webvpn_links(row("tandf", doi="10.1080/a"), VPN)[0],
                         f"https://www-tandfonline-com.{VPN}/doi/10.1080/a")
        self.assertEqual(litdl.vpn_host("onlinelibrary.wiley.com", VPN), f"https://onlinelibrary-wiley-com-443.{VPN}")

    def test_links_per_publisher(self):
        self.assertEqual(litdl.webvpn_links(row("wiley", doi="10.1111/a"), VPN),
                         (f"https://onlinelibrary-wiley-com-443.{VPN}/doi/10.1111/a", ""))
        self.assertEqual(litdl.webvpn_links(row("elsevier"), VPN), ("", ""))               # no PII: no address is made up
        page, pdf = litdl.webvpn_links(row("ieee", arnumber="123"), VPN)
        self.assertIn("/document/123", page)
        self.assertIn("arnumber=123", pdf)
        self.assertEqual(litdl.webvpn_links(row("wiley"), ""), ("", ""))                   # no WebVPN: no links

    def test_no_school_is_built_in(self):
        # no school's host may be written into the scripts or the starting holdings table; schools.json is the one
        # place that names schools, and only for the library path. (The empty-settings behaviour is tested below.)
        text = "".join(p.read_text(encoding="utf-8") for p in SCRIPTS.glob("*.py")) + \
               (SCRIPTS / "holdings.json").read_text(encoding="utf-8")
        hosts = set(re.findall(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.edu(?:\.[a-z]{2})?\b", text))
        self.assertEqual({h for h in hosts if not re.search(r"example|xxx|^a\.|^b\.", h)}, set())
        self.assertNotRegex(text, r"大学")


class Settings(unittest.TestCase):
    def test_webvpn_must_be_filled_in(self):
        with self.assertRaises(SystemExit) as cm:
            litdl.need_webvpn("")
        self.assertEqual(cm.exception.code, 2)                                           # 2 = needs the user
        self.assertEqual(litdl.need_webvpn("none"), "")
        self.assertEqual(litdl.need_webvpn(" webvpn.a.edu.cn "), "webvpn.a.edu.cn")

    def test_commands_stop_until_it_is(self):
        home = TMP / "fresh"
        home.mkdir(exist_ok=True)
        env = {"LITDL_WEBVPN": "", "LITDL_EBSCO_OPID": ""}
        rc, out = run("litdl.py", "config", home=home, env=env)
        self.assertEqual(rc, 2)
        self.assertIn("还没填", out)
        ids = home / "ids.txt"
        ids.write_text("10.1000/x\n", encoding="utf-8")
        rc, out = run("litdl.py", "plan", str(ids), "--out", str(home / "m.json"), home=home, env=env)
        self.assertNotEqual(rc, 0)
        self.assertIn("NEED_USER", out)
        self.assertFalse((home / "m.json").exists())
        rc, out = run("litbrowser.py", "login", "webvpn", home=home, env=env)
        self.assertIn("NEED_USER", out)

    def test_none_on_a_command_line_does_not_skip_the_question(self):
        # an agent once put --webvpn none on every command instead of asking; that has to stop like an empty setting
        home = TMP / "fresh2"
        home.mkdir(exist_ok=True)
        ids = home / "ids.txt"
        ids.write_text("10.1000/x\n", encoding="utf-8")
        env = {"LITDL_WEBVPN": "", "LITDL_EBSCO_OPID": ""}
        rc, out = run("litdl.py", "plan", str(ids), "--out", str(home / "m.json"), "--webvpn", "none", home=home, env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("不要自己在命令上加 --webvpn none", out)
        rc, out = run("litdl.py", "config", "--webvpn", "none", home=home, env=env)      # the user said: no WebVPN
        rc, out = run("litdl.py", "plan", str(ids), "--out", str(home / "m.json"), home=home, env=env)
        self.assertNotIn("NEED_USER", out)

    def test_config_keeps_only_the_host_and_rejects_nonsense(self):
        home = TMP / "cfg"
        home.mkdir(exist_ok=True)
        (home / ".env").write_text("# mine\nOTHER_KEY=keep\nLITDL_WEBVPN=old.edu.cn\n", encoding="utf-8")
        env = {"LITDL_WEBVPN": "", "LITDL_EBSCO_OPID": ""}
        rc, out = run("litdl.py", "config", "--webvpn", "https://webvpn.b.edu.cn/login?x=1", home=home, env=env)
        self.assertEqual(rc, 0, out)
        text = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("LITDL_WEBVPN=webvpn.b.edu.cn\n", text)
        self.assertIn("OTHER_KEY=keep", text)
        self.assertIn("# mine", text)
        self.assertNotIn("old.edu.cn", text)
        rc, out = run("litdl.py", "config", "--webvpn", "我们学校的vpn", home=home, env=env)
        self.assertNotEqual(rc, 0)


class Ledger(unittest.TestCase):
    def lines(self, stage, *recs):
        stage.mkdir(parents=True, exist_ok=True)
        with (stage / "results.jsonl").open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_only_answers_about_the_paper_count_as_tried(self):
        stage, rows = TMP / "stage1", [row("tandf", id=7)]
        self.lines(stage,
                   {"id": 7, "channel": "publisher", "outcome": "no_campus", "detail": "d", "at": "2026-01-01 10:00:00"},
                   {"id": 7, "channel": "ebsco", "outcome": "citation_only", "detail": "d", "at": "2026-01-01 10:01:00"},
                   {"id": 7, "channel": "scihub", "outcome": "error", "detail": "d", "at": "2026-01-01 10:02:00"})
        self.assertEqual(litdl.merge_results(rows, [stage]), 3)
        self.assertEqual(set(rows[0]["tried"]), {"ebsco"})
        self.assertEqual(set(rows[0]["stuck"]), {"publisher", "scihub"})     # EBSCO's answer did not wipe the others
        self.assertIn("publisher：", rows[0]["pending"])
        self.assertIn("scihub：", rows[0]["pending"])
        self.assertEqual(litdl.next_source(rows[0]), "scihub")               # off campus: past tandf; the error is retried
        self.assertIn("publisher", [c for c in litdl.required(rows[0]) if c not in rows[0]["tried"]])
        self.assertIn("⏸", litdl.route_text(rows[0]).split(" → ")[0])

    def test_an_answer_closes_only_its_own_channel(self):
        stage, rows = TMP / "stage3", [row("emerald", id=9)]
        self.lines(stage,
                   {"id": 9, "channel": "publisher", "outcome": "need_user", "detail": "出版商人机验证没人点", "at": "2026-01-01 10:00:00"},
                   {"id": 9, "channel": "ebsco", "outcome": "not_found", "detail": "", "at": "2026-01-01 10:01:00"})
        litdl.merge_results(rows, [stage])
        self.assertTrue(litdl.held_up(rows[0], "pubdoi"))
        self.assertEqual(litbrowser.pick_recipe(rows[0], "auto"), "scihub")
        self.lines(stage, {"id": 9, "channel": "publisher", "outcome": "no_access", "detail": "", "at": "2026-01-01 11:00:00"})
        litdl.merge_results(rows, [stage])
        self.assertNotIn("stuck", rows[0])
        self.assertNotIn("pending", rows[0])

    def test_a_filed_row_keeps_no_unfinished_note(self):
        r = row("informs", status="done", stuck={"public": "x"}, pending="public：x")
        litdl.show_stuck(r)
        self.assertNotIn("stuck", r)
        self.assertNotIn("pending", r)

    def test_each_line_is_applied_once(self):
        stage, rows = TMP / "stage2", [row("informs", id=3)]
        self.lines(stage, {"id": 3, "channel": "ebsco", "outcome": "not_found", "detail": "", "at": "2026-01-01 10:00:00"})
        self.assertEqual(litdl.merge_results(rows, [stage]), 1)
        rows[0]["tried"]["ebsco"] = "corrected by hand"
        self.assertEqual(litdl.merge_results(rows, [stage]), 0)
        self.assertEqual(rows[0]["tried"]["ebsco"], "corrected by hand")

    def test_doi_cleanup(self):
        self.assertEqual(litdl.clean_doi("10.1108/SCM-10-2021-0478]"), "10.1108/scm-10-2021-0478")
        self.assertEqual(litdl.clean_doi("10.1016/S0925-5273(15)00023-7."), "10.1016/s0925-5273(15)00023-7")
        self.assertEqual(litdl.clean_doi("10.1002/(SICI)1097-0266(199601)17:1"), "10.1002/(sici)1097-0266(199601)17:1")

    def test_titles_are_compared_the_same_way_on_both_sides(self):
        self.assertEqual(litdl.norm("Machine learning–enabled planning"), litdl.norm("machine learning enabled planning"))
        self.assertEqual(litdl.norm("The Research & Development"), "research and development")

    def test_version_label(self):
        v = litdl.version_of
        self.assertEqual(v("Coordinates have been deposited in the Protein Data Bank under accession 7XYZ."), "")
        self.assertIn("知识库", v("This version was deposited in the university research archive."))
        self.assertEqual(v("This is the author accepted manuscript of an article"), "录用稿")
        got = {"public": "已下到（2026-01-01）"}
        self.assertEqual(v("", row("nature", is_oa=True, tried=got)), "")                 # the publisher's own open page
        self.assertIn("非出版商", v("", row("informs", is_oa=True, tried=got)))            # a repository copy
        self.assertEqual(v("", row("informs", tried={"ebsco": "已下到（2026-01-01）"})), "")

    def test_a_repository_copy_of_the_typeset_paper_is_not_flagged(self):
        got = {"public": "已下到（2026-01-01）"}
        r = row("informs", doi="10.1287/msom.2022.1170", tried=got)
        self.assertEqual(litdl.version_of("MSOM 25(2) 543-562 https://doi.org/10.1287/msom.2022.1170", r), "")
        self.assertIn("非出版商", litdl.version_of("the paper, no header with its DOI", r))

    def test_rows_without_a_database_are_listed_apart(self):
        m = TMP / "nolib.json"
        full = {"ebsco": "x", "scihub": "x", "public": "x"}
        m.write_text(json.dumps([row("informs", id=1, tried=full, target="A.pdf"),
                                 row("informs", id=2, tried={"ebsco": "x"}, target="B.pdf"),
                                 row("unknown", id=3, title="", target="", doi="10.1234/nope")]), encoding="utf-8")
        rc, out = run("litdl.py", "missing", str(m), "--exhausted")
        self.assertIn("还有库没试的：1 篇", out)
        self.assertIn("没有可试的库：1 篇", out)
        self.assertIn("10.1234/nope", out)

    def test_a_guessed_abbreviation_already_in_use_is_flagged(self):
        m = litdl.load_abbr()
        ab, guessed = litdl.guess_abbr("Journal of Consumer Policy", m)      # not in the table, guessed JCP
        self.assertTrue(guessed)
        self.assertEqual(litdl.abbr_clash(ab, "Journal of Consumer Policy", m), "journal of consumer psychology")
        self.assertEqual(litdl.abbr_clash("JCP", "Journal of Consumer Psychology", m), "")    # its own entry is no clash

    def test_abbreviations_the_user_agreed_are_in_the_table(self):
        m = litdl.load_abbr()
        for journal, ab in (("Communications of the ACM", "CACM"), ("Journal of Cleaner Production", "JCLP"),
                            ("Transportation Research Part E: Logistics and Transportation Review", "TRE"),
                            ("Service Science", "SS")):
            self.assertEqual(litdl.guess_abbr(journal, m), (ab, False))


    def test_no_character_windows_refuses_is_left_in_a_file_name(self):
        r = {"title": "Does it matter (and why?)", "subtitle": "", "surname": "Lee", "abbr": "MS", "year_print": 2020}
        self.assertEqual(litdl.target_name(r), "Lee(20 MS)Does it matter (and why).pdf")
        r["title"] = "Who?\u2014A study: of things"
        self.assertEqual(litdl.target_name(r), "Lee(20 MS)Who-A study-of things.pdf")
        self.assertEqual(litdl.safe_filename('a:b?c*d"e<f>g|h.pdf'), "abcdefgh.pdf")

    def test_working_paper_counts_only_at_the_top(self):
        body = "Published article. " + "x " * 900 + "as shown in NBER Working Paper 12345"
        self.assertEqual(litdl.version_of(body), "")
        self.assertEqual(litdl.version_of("Working Paper No. 7 " + body), "工作论文")

    def test_file_name(self):
        r = {"surname": "Yang", "year_print": 2025, "abbr": "MKS", "title": "Engagement That Sells",
             "subtitle": "Influencer Video Advertising on TikTok"}
        self.assertEqual(litdl.target_name(r), "Yang(25 MKS)Engagement That Sells-Influencer Video Advertising on TikTok.pdf")


class Rename(unittest.TestCase):
    """LITDL rename: after a new abbreviation is agreed, filed rows get it; only the "(YY ABBR)" part changes."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(dir=TMP))
        self.lib = self.d / "lib"
        self.lib.mkdir()
        self.old = self.lib / "Krizhevsky(17 CA)ImageNet Classification with Deep Convolutional Neural Networks.pdf"
        self.old.write_bytes(b"%PDF-1.4 x")
        self.m = self.d / "manifest.json"
        self.m.write_text(json.dumps([
            {"id": 8, "journal": "Communications of the ACM", "abbr": "CA", "abbr_guess": True, "status": "done",
             "file": str(self.old), "target": self.old.name, "title": "x", "tried": {}},
            {"id": 9, "journal": "Service Science", "abbr": "SS", "abbr_guess": True, "status": "todo",
             "target": "Lim(20 SS)y.pdf", "title": "y", "tried": {}},
            {"id": 10, "journal": "Journal of Cleaner Production", "abbr": "JCP", "abbr_guess": True, "status": "todo",
             "target": "Seuring(08 JCP)z.pdf", "title": "z", "tried": {}},
            {"id": 11, "journal": "Journal of Consumer Policy", "abbr": "JCP", "abbr_guess": True, "status": "todo",
             "target": "Ng(19 JCP)w.pdf", "title": "w", "tried": {}}]), encoding="utf-8")

    def rows(self):
        return {r["id"]: r for r in json.loads(self.m.read_text(encoding="utf-8"))}

    def test_dry_run_touches_nothing(self):
        before = self.m.read_text(encoding="utf-8")
        rc, out = run("litdl.py", "rename", str(self.m), "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("RENAMED  #8", out)
        self.assertTrue(self.old.exists())
        self.assertEqual(self.m.read_text(encoding="utf-8"), before)

    def test_filed_rows_are_renamed_and_logged_and_guesses_reported(self):
        rc, out = run("litdl.py", "rename", str(self.m))
        self.assertEqual(rc, 0, out)
        new = self.lib / "Krizhevsky(17 CACM)ImageNet Classification with Deep Convolutional Neural Networks.pdf"
        self.assertTrue(new.exists())
        self.assertFalse(self.old.exists())
        r = self.rows()
        self.assertEqual((r[8]["abbr"], r[8]["abbr_guess"], r[8]["file"]), ("CACM", False, str(new)))
        self.assertEqual((r[9]["abbr"], r[9]["abbr_guess"]), ("SS", False))                  # confirmed, same name
        self.assertEqual((r[10]["abbr"], r[10]["target"]), ("JCLP", "Seuring(08 JCLP)z.pdf"))  # not filed: target only
        self.assertEqual((r[11]["abbr"], r[11]["abbr_guess"]), ("JCP", True))                # still a guess
        self.assertIn("still a guess", out)
        self.assertIn("\trename\t", (self.d / "ingest_log.tsv").read_text(encoding="utf-8"))


class Matching(unittest.TestCase):
    """Which database record, and which PDF, is this paper. 2026-11-21 a comment on a paper was taken for it."""
    W = litdl.norm("Information Distortion in a Supply Chain: The Bullwhip Effect")

    def rec(self, *titles):
        return [{"rec": str(i), "t": litdl.norm(x)} for i, x in enumerate(titles)]

    def test_a_comment_on_the_paper_is_not_the_paper(self):
        got = litdl.pick_title(self.W, self.rec(
            'Comments on "Information Distortion in a Supply Chain: The Bullwhip Effect"',
            "Information Distortion in a Supply Chain: The Bullwhip Effect"))
        self.assertEqual(got["rec"], "1")                                          # the exact title wins
        for x in ('Comments on "Information Distortion in a Supply Chain: The Bullwhip Effect"',
                  "Information Distortion in a Supply Chain: The Bullwhip Effect: Reflections",
                  "Reply to Information Distortion in a Supply Chain: The Bullwhip Effect",
                  "Erratum: Information Distortion in a Supply Chain: The Bullwhip Effect"):
            self.assertIsNone(litdl.pick_title(self.W, self.rec(x)), x)

    def test_harmless_differences_still_match(self):
        w = litdl.norm("Pricing with Limited Knowledge of Demand")
        self.assertIsNotNone(litdl.pick_title(w, self.rec("Technical Note—Pricing with Limited Knowledge of Demand")))
        w2 = litdl.norm("Supply Chain Resilience: A Review of the Literature and Future Directions")
        self.assertIsNotNone(litdl.pick_title(w2, self.rec("Supply Chain Resilience: A Review of the Literature")))
        self.assertIsNone(litdl.pick_title(w2, self.rec("Supply Chain Resilience")))   # too short to be sure

    def test_ingest_stops_when_page_one_carries_another_doi(self):
        p1 = "MANAGEMENT SCIENCE Vol. 50 doi 10.1287/mnsc.1040.0305 © 2004 INFORMS Comments on ..."
        self.assertEqual(litdl.other_dois(p1, "10.1287/mnsc.43.4.546"), ["10.1287/mnsc.1040.0305"])
        self.assertEqual(litdl.other_dois("doi 10.1287/mnsc.43.4.546 ...", "10.1287/mnsc.43.4.546"), [])
        # another version of the same work is not another article
        self.assertEqual(litdl.other_dois("SSRN https://doi.org/10.2139/ssrn.1234567", "10.1287/x"), [])
        self.assertEqual(litdl.other_dois("x" * 1600 + " 10.1016/j.ref.2001.01.001", "10.1287/x"), [])   # a reference
        # the row's own DOI broken over two lines in the extracted text (three false alarms in a real library)
        self.assertEqual(litdl.other_dois("doi 10.1287/mksc.\n1070.0303 ...", "10.1287/mksc.1070.0303"), [])

    def test_the_whole_ingest_refuses_such_a_file(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        d = TMP / "ing"
        (d / "src").mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        for i in range(8):
            pg = doc.new_page()
            if i == 0:
                pg.insert_text((50, 80), "MANAGEMENT SCIENCE doi 10.1287/mnsc.1040.0305 Lee Padmanabhan Whang")
                pg.insert_text((50, 110), 'Comments on "Information Distortion in a Supply Chain: The Bullwhip Effect"')
        doc.save(d / "src" / "5.pdf")
        m = d / "m.json"
        m.write_text(json.dumps([{"id": 5, "doi": "10.1287/mnsc.43.4.546", "bucket": "informs", "status": "todo",
                                  "title": "Information Distortion in a Supply Chain", "subtitle": "The Bullwhip Effect",
                                  "surname": "Lee", "abbr": "MS", "year_print": 1997, "target": "Lee(97 MS)x.pdf",
                                  "subdir": "", "tried": {}}]), encoding="utf-8")
        rc, out = run("litdl.py", "ingest", str(m), "--src", str(d / "src"), "--dest", str(d / "lib"))
        self.assertIn("SUSPECT", out)
        self.assertIn("10.1287/mnsc.1040.0305", out)
        self.assertFalse((d / "lib").exists() and any((d / "lib").iterdir()))


class Portal(unittest.TestCase):
    def test_how_database_entries_are_recognised(self):
        links = [
            {"url": f"https://onlinelibrary-wiley-com-443.{VPN}/", "raw": f"https://onlinelibrary-wiley-com-443.{VPN}/", "text": "Wiley Online Library"},
            {"url": f"https://link-springer-com.{VPN}/", "raw": f"https://link-springer-com.{VPN}:443/", "text": "Springer"},
            {"url": f"https://www-emerald-com-s.{VPN}/insight", "raw": f"https://www-emerald-com-s.{VPN}/insight", "text": "Emerald"},
            {"url": f"https://{VPN}/http/7772647670", "raw": f"https://{VPN}/http/7772647670", "text": "IEEE Electronic Library"},
            {"url": "https://www.tandfonline.com/", "raw": "https://www.tandfonline.com/", "text": "Taylor & Francis"},
            {"url": f"https://research-ebsco-com-443.{VPN}/c/abc123/search", "raw": "", "text": "EBSCO"},
            {"url": f"https://{VPN}/news/3", "raw": "", "text": "图书馆通知"},
        ]
        f = litbrowser.portal_scan(links, VPN)
        self.assertEqual(f["wiley"]["form"], "onlinelibrary-wiley-com-443.{vpn}")
        self.assertEqual(f["springer"]["form"], "link-springer-com.{vpn}:443")           # the port the page wrote
        self.assertEqual(f["emerald"]["form"], "www-emerald-com-s.{vpn}")
        self.assertEqual(f["ieee"]["form"], "")                                          # known by its label only
        self.assertEqual(f["tandf"]["form"], "直接链到官网")
        self.assertIn("ebsco", f)
        self.assertNotIn("cnki", f)

    def test_nothing_found_on_a_page_without_databases(self):
        links = [{"url": f"https://{VPN}/resource", "raw": "", "text": "外文资源"},
                 {"url": f"https://lib-example-edu-cn-443.{VPN}/", "raw": "", "text": "图书馆主页"}]
        self.assertEqual(litbrowser.portal_scan(links, VPN), {})

    def test_library_paths_are_listed_per_school(self):
        table = json.loads((SCRIPTS / "schools.json").read_text(encoding="utf-8"))
        for host, s in table.items():
            if host.startswith("_"):
                continue
            self.assertTrue({"school", "path", "resources"} <= set(s), host)
            self.assertIn("{vpn}", s["resources"])
        self.assertNotIn(VPN, table)                                                     # an unknown school gets a hint
        rc, out = run("litbrowser.py", "portal", "--library")
        self.assertEqual(rc, 2)
        self.assertIn("NEED_USER", out)


class Fetch(unittest.TestCase):
    def setUp(self):
        use_holdings()

    def test_auto_goes_to_the_next_database_without_an_answer(self):
        r = row("informs", alt_copies=["https://repo/x"])
        self.assertEqual(litbrowser.pick_recipe(r, "auto"), "ebsco")
        r["tried"] = {"ebsco": "x"}
        self.assertEqual(litbrowser.pick_recipe(r, "auto"), "scihub")
        r["tried"]["scihub"] = "x"
        self.assertEqual(litbrowser.pick_recipe(r, "auto"), "public")
        r["tried"]["public"] = "x"
        self.assertIsNone(litbrowser.pick_recipe(r, "auto"))

    def test_public_copy_only_when_there_is_a_link(self):
        r = row("informs", tried={"ebsco": "x", "scihub": "x"})
        self.assertIsNone(litbrowser.pick_recipe(r, "auto"))

    def test_campus_recipes_are_passed_over_once_off_campus(self):
        self.assertEqual(litbrowser.pick_recipe(row("tandf"), "auto", skip={"tandf"}), "ebsco")

    def test_a_row_stuck_at_the_publishers_own_site_moves_on_next_round(self):
        r = row("tandf", pending="publisher：等用户点人机验证（2026-01-01） T&F 人机验证没人点")
        self.assertEqual(litbrowser.pick_recipe(r, "auto"), "ebsco")
        self.assertNotIn("publisher", r["tried"])                                # still open: not exhausted
        self.assertIn("publisher", [c for c in litdl.required(r) if c not in r["tried"]])
        r2 = row("informs", pending="ebsco：被限流（2026-01-01）")                  # throttling is retried, not skipped
        self.assertEqual(litbrowser.pick_recipe(r2, "auto"), "ebsco")
        r3 = row("elsevier", webvpn_page="https://x/p", pii="S0925527315000237",
                 pending="publisher：等用户点人机验证（2026-01-01） ScienceDirect 的 PDF 页人机验证没通过")
        self.assertEqual(litbrowser.pick_recipe(r3, "auto"), "scihub")           # any database: a failed verification moves on
        r4 = row("tandf", pending="publisher：出版商没认出学校（不在校园网，不算试过）（2026-01-01）")
        self.assertEqual(litbrowser.pick_recipe(r4, "auto"), "ebsco")
        r5 = row("informs", pending="ebsco：出版商没认出学校")                      # only campus-IP recipes read that as "move on"
        self.assertEqual(litbrowser.pick_recipe(r5, "auto"), "ebsco")

    def test_addresses_are_built_when_fetching_not_read_from_the_plan(self):
        use_holdings({"webvpn_hosts": {"link.springer.com": "link-springer-com.{vpn}:443"}})
        env = type("E", (), {"vpn": VPN})()
        r = row("springer", doi="10.1007/s1", webvpn_page="https://link-springer-com-443.old/article/x",
                webvpn_pdf="https://link-springer-com-443.old/content/pdf/x.pdf")
        page, pdf = litbrowser.vpn_links(env, r)
        self.assertTrue(page.startswith(f"https://link-springer-com.{VPN}:443/"))
        self.assertTrue(pdf.endswith("/content/pdf/10.1007/s1.pdf"))

    def test_a_named_recipe_is_taken_as_is(self):
        self.assertEqual(litbrowser.pick_recipe(row("informs"), "scihub"), "scihub")

    def test_a_finished_row_goes_back_to_the_school_database(self):
        r = row("informs", status="done", tried={"ebsco": "x", "public": "x"})
        self.assertEqual(litbrowser.pick_recipe(r, "auto"), "ebsco")


class Ledger2(unittest.TestCase):
    def test_a_wrong_ledger_entry_can_be_removed(self):
        m = TMP / "untried.json"
        m.write_text(json.dumps([row("springer", id=3, tried={"public": "已下到（2026-01-01）", "publisher": "x"})]),
                     encoding="utf-8")
        rc, out = run("litdl.py", "mark", str(m), "--ids", "3", "--untried", "public")
        self.assertEqual(rc, 0, out)
        self.assertEqual(set(json.loads(m.read_text(encoding="utf-8"))[0]["tried"]), {"publisher"})


class RequestBatch(unittest.TestCase):
    def batch(self, rows, ids, budget):
        m = TMP / "rb.json"
        m.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return run("ablesci_assist.py", "request-batch", str(m), "--ids", ids, "--dest", str(TMP / "lib"),
                   "--budget", str(budget), "--dry-run")

    def rows(self):
        full = {"ebsco": "x", "scihub": "x", "public": "x"}
        return [row("informs", id=1, tried=dict(full)),
                row("informs", id=2, tried={"ebsco": "x"}),
                row("informs", id=3, tried=dict(full), status="done"),
                row("informs", id=4, tried={**full, "ablesci": "已发求助，还没人应助（2026-01-01，求助 id abc123）"}),
                row("informs", id=5, tried={"ebsco": "x", "ablesci": "已发求助，还没人应助（2026-01-01，求助 id zzz999）"})]

    def test_refuses_a_row_with_a_database_untried(self):
        rc, out = self.batch(self.rows(), "1,2", 100)
        self.assertNotEqual(rc, 0)
        self.assertIn("#2", out)
        self.assertIn("一篇都没有发", out)

    def test_refuses_a_row_that_is_already_there(self):
        rc, out = self.batch(self.rows(), "3", 100)
        self.assertNotEqual(rc, 0)
        self.assertIn("#3", out)

    def test_refuses_to_spend_more_than_agreed(self):
        rc, out = self.batch(self.rows(), "1", 0)
        self.assertNotEqual(rc, 0)
        self.assertIn("超过 --budget", out)

    def test_a_request_already_posted_is_never_posted_again(self):
        rc, out = self.batch(self.rows(), "1,4,5", 10)
        self.assertEqual(rc, 0, out)
        self.assertIn("要发 1 篇（10 积分）", out)
        self.assertIn("#4:abc123", out)
        self.assertIn("#5:zzz999", out)                                                  # even though its ledger is incomplete
        self.assertIn("DRY-RUN", out)

    def test_the_same_row_named_twice_is_posted_once(self):
        rc, out = self.batch(self.rows(), "1,1", 10)
        self.assertEqual(rc, 0, out)
        self.assertIn("要发 1 篇（10 积分）", out)

    def test_a_post_the_log_knows_and_the_ledger_does_not_stops_the_batch(self):
        # a crash after the publish click left no ledger entry; the log written right after the click still knows
        log = TMP / "home" / "assist" / "log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        rec = {"time": "2026-01-01 10:00:00", "cmd": "request", "doi": "10.1000/posted", "req": "q77", "outcome": "posted"}
        log.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        try:
            full = {"ebsco": "x", "scihub": "x", "public": "x"}
            rc, out = self.batch([row("informs", id=6, doi="10.1000/posted", tried=dict(full))], "6", 100)
            self.assertNotEqual(rc, 0)
            self.assertIn("流水里有这篇发布过的记录", out)
            self.assertIn("q77", out)
        finally:
            log.unlink()


class HandsOff(unittest.TestCase):
    """The hands-off verification path (2026-11-22: ScienceDirect first, then every verification), without a browser:
    the helpers, and the waiting loop run against a fake tab list, a fake clock and a mouse that only records."""

    def test_checkbox_iframes_are_found_inside_closed_shadow_roots(self):
        import captcha_ai
        tree = {"nodeName": "#document", "nodeId": 1, "children": [
            {"nodeName": "HTML", "nodeId": 2, "children": [
                {"nodeName": "DIV", "nodeId": 3, "shadowRoots": [
                    {"nodeName": "#document-fragment", "nodeId": 4, "children": [
                        {"nodeName": "IFRAME", "nodeId": 5,
                         "attributes": ["src", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"]}]}]},
                {"nodeName": "IFRAME", "nodeId": 6,
                 "attributes": ["src", "https://www.google.com/recaptcha/api2/anchor?size=invisible"]},
                {"nodeName": "IFRAME", "nodeId": 7, "attributes": ["src", "https://example.org/banner"]}]}]}
        self.assertEqual(captcha_ai.widget_nodes(tree), [("turnstile", 5)])

    def test_css_point_to_screen_pixel(self):
        import osmouse
        # a 1280x657 CSS viewport in a 2560x1440 client area at 200 %: tab strip and toolbar take 1440 - 1314 = 126 px
        self.assertEqual(osmouse.css_to_screen(100, 50, 2560, 1440, 1280, 657, 10, 20), (120, 50 + 126 + 40))
        with self.assertRaises(RuntimeError):
            osmouse.css_to_screen(0, 0, 100, 100, 0, 0, 1, 1)

    # ---- a covered window (another computer, the agent's window on top, 11-23): put on top only with the user's yes
    def _covered_point(self, allowed, on_top_after=True):
        from unittest import mock
        import osmouse
        seen, raised = iter([False, on_top_after]), []
        with mock.patch.object(osmouse, "IS_WIN", True), \
                mock.patch.object(osmouse, "_focus", lambda h: False), \
                mock.patch.object(osmouse, "_client", lambda h: (0, 0, 1280, 800)), \
                mock.patch.object(osmouse, "point_is_on", lambda h, x, y: next(seen)), \
                mock.patch.object(osmouse, "_user32", mock.Mock(IsIconic=mock.Mock(return_value=0))), \
                mock.patch.object(osmouse, "_SetWindowPos", lambda *a: raised.append(a[1]) or 1, create=True), \
                mock.patch.object(osmouse.time, "sleep", lambda s: None):
            osmouse.allow_raise(allowed)
            try:
                return osmouse._screen_point(42, 1280, 700, 100, 50), raised
            finally:
                osmouse.allow_raise(False)
                osmouse._RAISED.clear()

    def test_a_covered_window_is_left_to_the_user_without_consent(self):
        point, raised = self._covered_point(allowed=False)
        self.assertIsNone(point)
        self.assertEqual(raised, [])

    def test_a_covered_window_is_put_on_top_with_consent_then_clicked(self):
        import osmouse
        point, raised = self._covered_point(allowed=True)
        self.assertIsNotNone(point)
        self.assertEqual(raised, [osmouse.HWND_TOPMOST])

    def test_another_topmost_window_in_the_way_still_means_no_click(self):
        point, raised = self._covered_point(allowed=True, on_top_after=False)
        self.assertIsNone(point)

    def test_a_raised_window_is_put_back(self):
        from unittest import mock
        import osmouse
        calls = []
        with mock.patch.object(osmouse, "_SetWindowPos", lambda *a: calls.append(a[:2]) or 1, create=True):
            osmouse._RAISED.update({42, 43})
            osmouse.lower_all()
        self.assertEqual(sorted(calls), [(42, osmouse.HWND_NOTOPMOST), (43, osmouse.HWND_NOTOPMOST)])
        self.assertEqual(osmouse._RAISED, set())

    def test_consent_and_refusal_cannot_both_be_given(self):
        rc, out = run("litbrowser.py", "fetch", "m.json", "--stage", str(TMP / "st"), "--raise-window", "--no-auto-captcha")
        self.assertEqual(rc, 2)
        self.assertIn("contradict", out)
        for sub in ("fetch", "get", "solve", "press"):
            self.assertIn("--raise-window", run("litbrowser.py", sub, "--help")[1], sub)

    def test_verification_titles(self):
        for t in ("请稍候…", "Just a moment...", "Security verification", "Are you a robot?"):
            self.assertTrue(litbrowser.CHALLENGE_TITLE.search(t), t)
        for t in ("How big data can make big impact - ScienceDirect", "中国石油大学 (华东) WebVPN", ""):
            self.assertFalse(litbrowser.CHALLENGE_TITLE.search(t), t)

    def test_only_new_finished_files_are_taken(self):
        d = TMP / "dl"
        d.mkdir(exist_ok=True)
        old, litbrowser.DL_DIR = litbrowser.DL_DIR, d
        try:
            (d / "old.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            before = {p.name for p in d.iterdir()}
            (d / "main.pdf.crdownload").write_bytes(b"%PDF-1.4")
            self.assertEqual(litbrowser.finished_downloads(before), ([], True))     # still being written
            (d / "main.pdf.crdownload").rename(d / "main.pdf")
            got, writing = litbrowser.finished_downloads(before)
            self.assertEqual(([p.name for p in got], writing), (["main.pdf"], False))
        finally:
            litbrowser.DL_DIR = old

    def test_with_the_papers_own_name_another_file_is_never_taken(self):
        # review 2026-11-22: a file that was still a .crdownload when the paper started, or one another tab finished
        # meanwhile, must not be filed as this paper
        d = TMP / "dl3"
        d.mkdir(exist_ok=True)
        old, litbrowser.DL_DIR = litbrowser.DL_DIR, d
        try:
            (d / "1-s2.0-S0000000000000001-main.pdf.crdownload").write_bytes(b"%PDF-1.4")
            before = {p.name for p in d.iterdir()}
            (d / "1-s2.0-S0000000000000001-main.pdf.crdownload").rename(d / "1-s2.0-S0000000000000001-main.pdf")
            (d / "Some paper(ablesci.com).pdf").write_bytes(b"%PDF-1.4")
            got, _ = litbrowser.finished_downloads(before, want="S0000000000000002")
            self.assertEqual(got, [])
            (d / "1-s2.0-S0000000000000002-main.pdf").write_bytes(b"%PDF-1.4")
            got, _ = litbrowser.finished_downloads(before, want="S0000000000000002")
            self.assertEqual([p.name for p in got], ["1-s2.0-S0000000000000002-main.pdf"])
        finally:
            litbrowser.DL_DIR = old

    def test_a_page_instead_of_a_pdf_is_not_taken(self):
        d = TMP / "dl2"
        d.mkdir(exist_ok=True)
        (d / "x.pdf").write_text("<html>Are you a robot?</html>", encoding="utf-8")
        res = litbrowser.take_download(d / "x.pdf", d / "9.pdf")
        self.assertEqual(res["outcome"], "not_pdf")
        self.assertFalse((d / "9.pdf").exists())

    def test_what_a_sciencedirect_pdf_tab_says(self):
        r = litbrowser.sd_judge("There was a problem providing the content you requested ... IP: 104.0.0.1")
        self.assertEqual(r["outcome"], "blocked")
        self.assertIn("104.0.0.1", r["detail"])
        self.assertEqual(litbrowser.sd_judge("Get Access  Purchase PDF")["outcome"], "no_access")
        self.assertIsNone(litbrowser.sd_judge("Request Verification: In Progress"))

    def test_letting_go_marks_old_pages_as_dead(self):
        s = litbrowser.Session(autostart=False)
        s.release()                                                                   # nothing attached yet: no error
        self.assertEqual(s.generation, 1)
        self.assertIsNone(s.ctx)

    def test_the_switch(self):
        os.environ["LITDL_HANDSOFF"] = "0"
        try:
            self.assertFalse(litbrowser.handsoff_ok())
        finally:
            del os.environ["LITDL_HANDSOFF"]
        os.environ["LITDL_HANDSOFF"] = "0"
        try:
            self.assertIn("老做法", litbrowser.handsoff_describe())
        finally:
            del os.environ["LITDL_HANDSOFF"]
        self.assertIn("websockets", litbrowser.handsoff_describe())

    def test_an_older_websockets_still_connects(self):
        # websockets 11-14 have no `proxy` argument: on another computer every verification went to the user (11-23)
        from unittest import mock
        import websockets.sync.client
        calls = []

        def old_connect(url, **kw):
            calls.append(kw)
            if "proxy" in kw:
                raise TypeError("create_connection() got an unexpected keyword argument 'proxy'")
            return mock.Mock()

        with mock.patch.object(websockets.sync.client, "connect", old_connect):
            with litbrowser.BareTab("ws://127.0.0.1:1/devtools/page/x") as t:
                self.assertIsNotNone(t.ws)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("proxy", calls[1])
        self.assertEqual(calls[1]["max_size"], 2 ** 25)

    # ---- every verification goes hands off (user, 2026-11-22: "如果遇到验证，都用这个方式过")
    def test_scihubs_robot_page_is_a_verification(self):
        for t in ("你是机器人吗？", "Вы робот?"):
            self.assertTrue(litbrowser.CHALLENGE_TITLE.search(t), t)
        self.assertFalse(litbrowser.CHALLENGE_TITLE.search("机器人路径规划研究 - 某期刊"))

    def test_a_page_is_judged_from_its_tree_when_the_title_says_nothing(self):
        box = {"kind": "turnstile", "x": 10, "y": 10, "w": 300, "h": 65}
        bc = litbrowser.bare_challenge
        self.assertTrue(bc("Just a moment..."))
        self.assertTrue(bc("papers.ssrn.com", ([box], 1280, 700, "Verify you are human")))
        self.assertTrue(bc("SSRN", ([], 1280, 700, "Checking your browser before accessing")))
        self.assertFalse(bc("A Paper - Publisher", ([], 1280, 700, "Abstract " * 400 + "人机验证")))
        self.assertFalse(bc("A Paper - Publisher", ([box], 1280, 700, "Abstract " * 700)))   # a widget inside a long page
        self.assertFalse(bc("SSRN", ([box], 1280, 700, "验证成功。正在等待 papers.ssrn.com 响应")))
        self.assertFalse(bc("A Paper - Publisher"))
        # SSRN's "sign up first" page: the box belongs to the sign-up form, the way on is its own button (11-22, #157)
        gate = "SSRN Alert Create an SSRN Account Email Address Password Continue and Download Download without registration"
        self.assertFalse(bc("SSRN Alert", ([box], 1234, 1277, gate)))

    def test_the_page_text_is_not_cut_short_before_it_is_judged(self):
        # bare_look used to keep only 3000 characters, so "a widget on a long page is not a verification" never held
        from unittest import mock
        page = "<html><body>" + "<p>Abstract text of the paper.</p>" * 400 + "</body></html>"

        class Fake:
            def __init__(self, ws):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                pass

            def send(self, method, params=None):
                return {"DOM.getDocument": {"root": {"nodeName": "#document", "nodeId": 1}},
                        "Page.getLayoutMetrics": {"cssVisualViewport": {"clientWidth": 1280, "clientHeight": 700}},
                        "DOM.getOuterHTML": {"outerHTML": page}}[method]

        with mock.patch.object(litbrowser, "BareTab", Fake):
            look = litbrowser.bare_look("ws://fake")
        self.assertGreater(len(look[3]), 5000)

    def test_scihubs_own_button_is_found_and_clicked_in_its_middle(self):
        import captcha_ai
        tree = {"nodeName": "#document", "nodeId": 1, "children": [
            {"nodeName": "BUTTON", "nodeId": 2, "attributes": ["onclick", "check()", "class", "b"]},
            {"nodeName": "BUTTON", "nodeId": 3, "attributes": ["onclick", "search()"]}]}
        self.assertEqual(litbrowser.verify_buttons(tree), [("button", 2)])
        self.assertEqual(litbrowser.hit_point({"kind": "button", "x": 100, "y": 200, "w": 80, "h": 30}), (140, 215))
        tb = {"kind": "turnstile", "x": 176.5, "y": 280, "w": 300, "h": 65}
        self.assertEqual(litbrowser.hit_point(tb), captcha_ai._checkbox_point("turnstile", tb))

    def test_a_bare_screenshot_is_planned_in_css_pixels(self):
        import struct
        from unittest import mock
        import captcha_ai
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 2560, 1400) + b"\x08\x02\x00\x00\x00"
        shot = captcha_ai.shot_from_bytes(png, 1280, 700)          # a 200 % screen: two image pixels per CSS pixel
        self.assertEqual((shot["iw"], shot["cw"], shot["sx"]), (2560, 1280, 0.5))
        with mock.patch.object(captcha_ai, "_ai_json",
                               return_value=({"type": "click", "points": [[400, 200]], "submit": None}, None)):
            plan, err = captcha_ai.plan_shot(shot, "t", "u", "b", {})
        self.assertEqual((plan["type"], plan["points"]), ("click", [[200.0, 100.0]]))

    def test_the_address_is_reopened_without_cloudflares_tokens(self):
        u = "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4346526&__cf_chl_rt_tk=abc.def-1"
        self.assertEqual(litbrowser.reopen_url(u), "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4346526")
        self.assertEqual(litbrowser.reopen_url("https://x.org/a?b=1"), "https://x.org/a?b=1")

    def test_a_paper_meets_a_verification_only_so_often(self):
        env = type("E", (), {"passes": litbrowser.HANDSOFF_PASSES, "sess": None, "prog": None, "human_wait": 1})()
        page, res = litbrowser.get_past(env, "page", "测试", TMP / "1.pdf")
        self.assertEqual(res["outcome"], "need_user")              # nothing was touched: there is no browser here

    def test_with_the_switch_off_the_attached_wait_is_used(self):
        from unittest import mock
        env = type("E", (), {"passes": 0, "sess": None, "prog": None, "human_wait": 1})()
        os.environ["LITDL_HANDSOFF"] = "0"
        try:
            with mock.patch.object(litbrowser, "wait_human", return_value=True):
                self.assertEqual(litbrowser.get_past(env, "page", "x", TMP / "1.pdf"), ("page", None))
            with mock.patch.object(litbrowser, "wait_human", return_value=False):
                self.assertEqual(litbrowser.get_past(env, "page", "x", TMP / "1.pdf")[1]["outcome"], "need_user")
        finally:
            del os.environ["LITDL_HANDSOFF"]

    def _run_handsoff(self, title_at, look, clicks_ok=True, human_wait=30, drop_file_at=None, enabled=True,
                      raise_ok=False):
        """handsoff_fetch against a fake browser and a fake clock: nothing real is opened, clicked or waited for."""
        from unittest import mock
        import captcha_ai
        import osmouse
        clock, calls = [1000.0], {"clicks": [], "closed": [], "asked": [], "raised": [], "lowered": 0}
        d = TMP / "dl-sim"
        d.mkdir(exist_ok=True)
        for f in d.iterdir():
            f.unlink()

        class FakeTime:
            @staticmethod
            def time():
                return clock[0]

            @staticmethod
            def sleep(s):
                clock[0] += s
                if drop_file_at is not None and clock[0] - 1000 >= drop_file_at and not (d / "x.pdf").exists():
                    (d / "x.pdf").write_bytes(b"%PDF-1.4\n%%EOF")

        def devtools(path, method="GET"):
            if path.startswith("/json/new"):
                return {"id": "TAB0000000000001"}
            if path.startswith("/json/close/"):
                calls["closed"].append(path.rsplit("/", 1)[1])
            return {}

        def tab_info(tid):
            return {"id": tid, "title": title_at(clock[0] - 1000), "url": "https://site.example/doc/1",
                    "webSocketDebuggerUrl": "ws://fake"}

        prog = type("P", (), {"need_user": lambda self, why, url, tab=None: calls["asked"].append((why, tab))})()
        env = type("E", (), {"prog": prog, "human_wait": human_wait})()
        cfg = {"enabled": enabled, "ready": False, "attempts": 3}
        with mock.patch.object(litbrowser, "time", FakeTime), mock.patch.object(litbrowser, "DL_DIR", d), \
                mock.patch.object(litbrowser, "devtools", devtools), mock.patch.object(litbrowser, "tab_info", tab_info), \
                mock.patch.object(litbrowser, "bare_look", lambda ws: look(clock[0] - 1000)), \
                mock.patch.object(litbrowser, "pdf_problem", lambda p: ""), \
                mock.patch.object(captcha_ai, "config", lambda home=None: cfg), \
                mock.patch.object(captcha_ai, "_os_mouse_enabled", lambda: True), \
                mock.patch.object(osmouse, "available", lambda: True), \
                mock.patch.object(osmouse, "window_for", lambda pid, title="": 42), \
                mock.patch.object(osmouse, "focus", lambda hwnd: True), \
                mock.patch.object(osmouse, "raise_allowed", lambda: raise_ok), \
                mock.patch.object(osmouse, "raise_window", lambda hwnd: calls["raised"].append(hwnd) or True), \
                mock.patch.object(osmouse, "lower_all", lambda: calls.__setitem__("lowered", calls["lowered"] + 1)), \
                mock.patch.object(osmouse, "click_css", lambda *a, **k: calls["clicks"].append(a[3:5]) or clicks_ok):
            res = litbrowser.handsoff_fetch(env, "https://site.example/doc/1", TMP / "sim.pdf", 1234, "测试验证",
                                            litbrowser.HANDSOFF_LIMIT, until_clear=True, replace="OLD")
        return res, calls

    def test_a_reopened_page_that_moves_on_is_handed_back_open(self):
        box = {"kind": "turnstile", "x": 0, "y": 0, "w": 300, "h": 65}
        res, calls = self._run_handsoff(
            lambda t: "Just a moment..." if t < 15 else "A Paper - Publisher",
            lambda t: ([box], 1280, 700, "") if t < 15 else ([], 1280, 700, "Abstract " * 500))
        self.assertEqual((res["outcome"], res["tab"]), ("passed", "TAB0000000000001"))
        self.assertEqual(len(calls["clicks"]), 1)                 # one click after the widget had been up a while
        self.assertEqual(calls["closed"], ["OLD"])                # the spoilt attached tab goes, the new one stays
        self.assertEqual(calls["asked"], [])

    def test_a_page_that_never_moves_on_is_clicked_twice_then_left_to_the_user(self):
        box = {"kind": "turnstile", "x": 0, "y": 0, "w": 300, "h": 65}
        res, calls = self._run_handsoff(lambda t: "请稍候…", lambda t: ([box], 1280, 700, ""))
        self.assertEqual(res["outcome"], "need_user")
        self.assertEqual(len(calls["clicks"]), litbrowser.HANDSOFF_PER_STEP)
        self.assertEqual(calls["asked"][0][1], "TAB000")          # the user is told which tab
        self.assertIn("TAB0000000000001", calls["closed"])

    def test_a_file_that_arrives_while_let_go_is_the_answer(self):
        res, calls = self._run_handsoff(lambda t: "Just a moment...", lambda t: ([], 1280, 700, ""), drop_file_at=8)
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue((TMP / "sim.pdf").exists())
        (TMP / "sim.pdf").unlink()

    def test_with_the_captcha_switch_off_nothing_is_clicked(self):
        box = {"kind": "turnstile", "x": 0, "y": 0, "w": 300, "h": 65}
        res, calls = self._run_handsoff(lambda t: "请稍候…", lambda t: ([box], 1280, 700, ""), enabled=False)
        self.assertEqual(calls["clicks"], [])
        self.assertEqual(res["outcome"], "need_user")
        self.assertTrue(calls["asked"])                           # asked at once, with the browser let go
        self.assertEqual(calls["raised"], [])                     # the user said no: nothing is put on top

    def test_with_consent_the_window_goes_on_top_once_and_comes_down_after(self):
        box = {"kind": "turnstile", "x": 0, "y": 0, "w": 300, "h": 65}
        res, calls = self._run_handsoff(
            lambda t: "Just a moment..." if t < 15 else "A Paper - Publisher",
            lambda t: ([box], 1280, 700, "") if t < 15 else ([], 1280, 700, "Abstract " * 500), raise_ok=True)
        self.assertEqual(res["outcome"], "passed")
        self.assertEqual(calls["raised"], [42])                   # once, as soon as the verification showed
        self.assertEqual(calls["lowered"], 1)                     # and put back when the page was done
        res, calls = self._run_handsoff(lambda t: "Just a moment...", lambda t: ([box], 1280, 700, ""))
        self.assertEqual(calls["raised"], [])                     # no consent given: never raised


class Docs(unittest.TestCase):
    """Every `LITA / LITB / LITDL <subcommand> --flag` in the docs must exist; every table row must fit its table."""
    NAMES = {"LITA": "ablesci_assist.py", "LITB": "litbrowser.py", "LITDL": "litdl.py"}
    FILES = [ROOT / "SKILL.md"] + sorted((ROOT / "references").glob("*.md"))

    @classmethod
    def setUpClass(cls):
        cls.subs, cls.flags, cls.top = {}, {}, {}
        for name, script in cls.NAMES.items():
            h = run(script, "--help")[1]
            cls.subs[name] = set(re.search(r"\{([a-z\-,]+)\}", h).group(1).split(","))
            cls.top[name] = set(re.findall(r"(--[a-z][a-z0-9\-]*)", h))
            for s in cls.subs[name]:
                cls.flags[(name, s)] = set(re.findall(r"(--[a-z][a-z0-9\-]*)", run(script, s, "--help")[1]))

    def test_commands_in_the_docs_exist(self):
        bad, n = [], 0
        for d in self.FILES:
            for no, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
                for m in re.finditer(r"\b(LITA|LITB|LITDL)((?: --[a-z\-]+)*) ([a-z][a-z\-]+)([^`#\n]*)", line):
                    name, pre, sub, rest = m.groups()
                    if sub not in self.subs[name]:
                        bad.append(f"{d.name}:{no} {name} {sub}: no such subcommand")
                        continue
                    n += 1
                    bad += [f"{d.name}:{no} {name} {f}: not a global option"
                            for f in re.findall(r"(--[a-z][a-z0-9\-]*)", pre) if f not in self.top[name]]
                    bad += [f"{d.name}:{no} {name} {sub} {f}: no such option"
                            for f in re.findall(r"(?<![\w\-])(--[a-z][a-z0-9\-]*)", rest)
                            if f not in self.flags[(name, sub)] and f not in self.top[name]]
        self.assertEqual(bad, [])
        self.assertGreater(n, 100)

    def test_tables_are_well_formed(self):
        bad = []
        for d in self.FILES:
            width = None
            for no, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
                if line.startswith("|"):
                    cells = len(re.split(r"(?<!\\)\|", line.strip())) - 2
                    if width is None:
                        width = cells
                    elif cells != width:
                        bad.append(f"{d.name}:{no} has {cells} cells, its table has {width}")
                else:
                    width = None
        self.assertEqual(bad, [])

    def test_data_files_parse(self):
        for f in SCRIPTS.glob("*.json"):
            json.loads(f.read_text(encoding="utf-8-sig"))

    def test_scripts_named_in_setup_exist(self):
        setup = (ROOT / "references" / "setup.md").read_text(encoding="utf-8")
        for f in list(SCRIPTS.glob("*.py")) + list(SCRIPTS.glob("*.json")):
            self.assertIn(f.name, setup, f"{f.name} is not listed in references/setup.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
