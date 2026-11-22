#!/usr/bin/env python3
"""ablesci_assist - both sides of 科研通 (ablesci.com), the paper-exchange site: ask for papers, and answer others.

Account
  points                       current balance and the latest entries of the points history
  signin [--dry-run]           the daily sign-in (+10, sometimes +20), once a day; says ALREADY when done today

Asking (spends the user's points: 10 per request, only with the user's consent for that batch)
  request --doi D --title T    post ONE request, wait for somebody to upload, download the file on the free line
                               into ~/.litdl/downloads. --dry-run stops before publishing. Never adopts
  fetch-upload --req ID        a request that got its file after `request` stopped waiting: download it
  adopt --req ID               accept the uploaded file - only after `litdl.py ingest` printed FILED for it
  request-batch MANIFEST --ids 1,2 --dest LIB --budget N
                               the three steps above for several manifest rows, one row at a time:
                               request -> ingest -> mark -> adopt. Refuses rows whose ledger does not show every
                               channel tried, refuses to spend more than --budget, never reposts a row that
                               already has a request, and stops at the first step that does not come out clean

Answering (earns points)
  scan  [--pages 3] [--max 8]  newest open requests ("求助中", no file yet, not a supplement request) whose paper
                               OpenAlex lists as open access under a licence that allows redistribution. Read-only
  assist --req URL --doi DOI   one request: download the OA copy, verify it, check the request is still open,
                               upload it. --dry-run does everything except choosing the file and uploading

What may be uploaded is decided here, not by the caller: the file always comes from an open-access location
OpenAlex gives for the DOI, under one of the licences in FREE (or NC_ND with --allow-nc). There is no option
to upload a local file, so a PDF fetched through a subscription cannot go out this way.

Runs in the dedicated Chrome of litbrowser.py (the user logs in to 科研通 there: `litbrowser.py login ablesci`).
One request at a time; points for an answer arrive only after the requester confirms the file.
Everything done is appended to ~/.litdl/assist/log.jsonl.
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import litdl  # noqa: E402
from litbrowser import HOME, Session, nav_fetch, pdf_problem, say  # noqa: E402

SITE = "https://www.ablesci.com"
OUT = HOME / "assist"
FREE = {"cc-by", "cc-by-sa", "cc0", "public-domain"}
NC_ND = {"cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd", "cc-by-nd"}
MIN_PAGES, MIN_TITLE = 3, 0.6

LIST_JS = r"""() => [...document.querySelectorAll('li')].map(li => {
  const a = li.querySelector('a.assist-list-title, a[href*="assist/detail"]');
  if (!a) return null;
  const fid = li.querySelector('.assist-file-id-val');
  const t = (li.innerText || '').replace(/\n+/g, ' ');
  const m = t.match(/(\d+)\s*(求助中|待确认)/);
  return {href: a.href.split('#')[0], status: m ? m[2] : '', points: m ? parseInt(m[1], 10) : 0,
          file_id: fid ? fid.value : null, text: t.slice(0, 140)};
}).filter(Boolean)"""

DETAIL_JS = r"""() => {
  const b = document.body ? document.body.innerText : '';
  return {title: document.title,
          dois: [...new Set((b.match(/10\.\d{4,9}\/[-._;()\/:A-Za-z0-9]+/g) || []))].slice(0, 5),
          supplement: /求助补充材料|补充材料/.test(b)};
}"""

TOAST_JS = r"""() => [...document.querySelectorAll('.layui-layer-content,.layui-layer-msg,.layui-layer-dialog')]
  .map(e => e.innerText.trim()).filter(Boolean).slice(-1)[0] || ''"""


def allowed(a):
    return FREE | NC_ND if a.allow_nc else FREE


_VIA_BROWSER = False


def openalex(doi, ctx=None):
    """-> dict(title, venue, copies=[(licence, pdf_url)]) - every OA location that carries a licence and a PDF.
    ctx: the dedicated Chrome's context. Used when this process cannot reach the API itself (seen 2026-11-21:
    the router answered no DNS query, Python and curl resolved nothing, the browser still worked)."""
    global _VIA_BROWSER
    url = "https://api.openalex.org/works/https://doi.org/" + urllib.parse.quote(doi, safe="/")
    d = None
    if not _VIA_BROWSER:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": litdl.UA})
            d = json.load(litdl.direct_opener().open(req, timeout=25))     # never through the system proxy
        except urllib.error.HTTPError:
            raise                             # the API answered (404 = no record): the browser would hear the same
        except OSError:                       # URLError, DNS failure, timeout
            if ctx is None:
                raise
            _VIA_BROWSER = True
            say("  这个进程连不上 OpenAlex，改由专用 Chrome 查询")
    if d is None:
        p = ctx.new_page()
        try:
            r = p.goto(url, wait_until="domcontentloaded", timeout=45000)
            if r is not None and r.status == 404:
                raise urllib.error.HTTPError(url, 404, "no OpenAlex record", None, None)
            d = json.loads(p.evaluate("() => document.body.innerText"))
        finally:
            p.close()
    copies, seen = [], set()
    for loc in [d.get("best_oa_location") or {}] + (d.get("locations") or []):
        lic, pdf = (loc.get("license") or "").lower(), loc.get("pdf_url")
        if lic and pdf and pdf not in seen and loc.get("is_oa", True):
            seen.add(pdf)
            copies.append((lic, pdf))
    return {"title": d.get("title") or "", "copies": copies,
            "venue": ((d.get("primary_location") or {}).get("source") or {}).get("display_name") or ""}


SUPP_HEAD = re.compile(r"supplementa(?:ry|l)\s+(?:material|information|data|file|table|figure|appendix|methods|text)"
                       r"|electronic\s+supplementary|supporting\s+information|multimedia\s+appendix"
                       r"|check\s?list|submission/publication\s+form|online\s+resource\s+\d", re.I)


def article_check(path, title):
    """-> (pages, score, why_not). The title has to be on page 1 itself, and page 1 must not open like a supplement.
    A reporting checklist or appendix repeats the article title a page or two in: on 2026-11-19 a 56-page
    CONSORT form filed under the article's DOI passed a first-two-pages title check, was uploaded, and the
    requester rejected it ("不是正文")."""
    pages, p1 = litdl.pdf_text(path, 1)
    words = [w for w in litdl.norm(title).split() if len(w) > 3]
    n1 = litdl.norm(p1)
    score = sum(w in n1 for w in words) / len(words) if words else 0.0
    why = ""
    if pages < MIN_PAGES:
        why = f"只有 {pages} 页"
    elif score < MIN_TITLE:
        why = f"首页上找不到文章标题（吻合 {score:.0%}）"
    elif SUPP_HEAD.search(p1[:150]):          # only the opening lines: ACS prints a 'Supporting Information' link under the authors
        why = "首页开头像补充材料或检查表"
    return pages, score, why


def landing(url):
    """PMC: OpenAlex lists .../articles/PMC123/pdf/ , which serves an interstitial without a PDF link. The article
    page itself carries citation_pdf_url with the real file name, and nav_fetch follows that."""
    return re.sub(r"(pmc\.ncbi\.nlm\.nih\.gov/articles/PMC\d+)/pdf/?$", r"\1/", url)


def same_doi(a, b):
    return a.lower().rstrip(".,;)") == b.lower().rstrip(".,;)")


def logged_in(page):
    """Hard signal: the points page is only served to a logged-in account. -> balance (int) or None"""
    page.goto(f"{SITE}/my/point", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    if "/site/login" in page.url:
        return None
    m = re.search(r"总积分为[：:]\s*(\d+)", page.evaluate("() => document.body ? document.body.innerText : ''"))
    return int(m.group(1)) if m else None


def need_login(page):
    bal = logged_in(page)
    if bal is None:
        sys.exit("NEED_USER  科研通没登录：先跑 litbrowser.py login ablesci --wait 300，由用户在专用 Chrome 里登录。")
    return bal


def remember(memo, rid):
    """One request id per line; scan skips these next time, so repeated scans only open requests that are new."""
    OUT.mkdir(parents=True, exist_ok=True)
    with memo.open("a", encoding="utf-8") as f:
        f.write(rid + "\n")


def log(rec):
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
    with (OUT / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- commands
def cmd_points(a):
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            bal = need_login(page)
            text = page.evaluate("() => document.body.innerText")
            say(f"当前积分：{bal}")
            for line in re.findall(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s+[+-]\d+\s+[^\n]+", text)[:a.last]:
                say("  " + re.sub(r"\s+", " ", line))
        finally:
            page.close()
    return 0


SIGN_JS = r"""() => {
  const box = document.querySelector('.fly-signin');
  if (!box) return {box: null, button: ''};
  const btn = [...box.querySelectorAll('button, a, input[type=button], .layui-btn')].find(e => {
    const t = (e.innerText || e.value || '').trim();
    return /打卡|签到/.test(t) && !/已连续|已签到|已打卡/.test(t);
  });
  if (btn) btn.setAttribute('data-litdl-sign', '1');
  return {box: box.innerText.replace(/\s+/g, ' ').trim().slice(0, 120),
          button: btn ? (btn.innerText || btn.value || '').trim() : ''};
}"""


def signed_today(text):
    """-> (time, points) of today's sign-in line in the points history, or None"""
    m = re.search(time.strftime("%Y-%m-%d") + r" (\d\d:\d\d:\d\d)\s+\+(\d+)\s+签到", text)
    return (m.group(1), int(m.group(2))) if m else None


def cmd_signin(a):
    """The daily sign-in. Whether today's is done is read from the points history, before and after: once signed,
    the home page shows no button at all (only "已连续签到 N 天"), so the button says nothing about success."""
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            bal = need_login(page)
            got = signed_today(page.evaluate("() => document.body.innerText"))
            if got:
                say(f"ALREADY  今天 {got[0]} 已经签过（+{got[1]}），余额 {bal}")
                return 0
            page.goto(SITE + "/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            s = page.evaluate(SIGN_JS)
            if not s["button"]:
                say(f"CHECK  首页上没找到签到按钮，没有点。签到栏里写的是：{s['box']!r}")
                return 1
            if a.dry_run:
                say(f"DRY-RUN  今天还没签到，会点“{s['button']}”。没有点。余额 {bal}")
                return 0
            page.click('[data-litdl-sign="1"]')
            page.wait_for_timeout(2500)
            tip = page.evaluate(LAYER_JS)
            try:
                page.click(".layui-layer-btn0, .layui-layer-close", timeout=2500)
            except Exception:
                pass
            new = logged_in(page)
            got = signed_today(page.evaluate("() => document.body.innerText"))
            log({"cmd": "signin", "outcome": "signed" if got else "unclear", "tip": tip[:120]})
            if not got:
                say(f"CHECK  点了“{s['button']}”，积分记录里还没有今天的签到。页面提示：{tip[:100]!r}。先看页面，不要连着点。")
                return 1
            say(f"SIGNED  +{got[1]}，余额 {bal} → {new}")
            return 0
        finally:
            page.close()


def cmd_scan(a):
    ok = allowed(a)
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            say(f"当前积分：{need_login(page)}")
            items = []
            for n in range(1, a.pages + 1):
                try:
                    page.goto(f"{SITE}/assist/index?status=waiting&page={n}", wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    items += page.evaluate(LIST_JS)
                except Exception as e:
                    say(f"列表第 {n} 页读不到：{str(e)[:60]}")
            seen, todo = set(), []
            skip = {x.strip() for x in a.skip.split(",") if x.strip()}
            memo = OUT / "scanned.txt"        # requests already looked up and found to have no eligible copy
            if memo.exists() and not a.rescan:
                skip |= set(memo.read_text(encoding="utf-8").split())
            for it in items:
                if it["href"].split("id=")[-1] in skip:
                    continue
                if it["href"] not in seen and it["status"] == "求助中" and not it["file_id"]:
                    seen.add(it["href"])
                    todo.append(it)
            if a.by_points:                   # default: list order = newest first, the ones nobody has answered yet
                todo.sort(key=lambda x: x["points"], reverse=True)
            say(f"求助中、还没人上传的：{len(todo)} 条；逐条查许可证（最多看 {a.visit} 条，找够 {a.max} 条就停）")
            found = errs = 0
            for it in todo[:a.visit]:
                if found >= a.max:
                    break
                try:
                    page.goto(it["href"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    d = page.evaluate(DETAIL_JS)
                    doi = next(iter(d["dois"]), None)
                    rid = it["href"].split("id=")[-1]
                    if "求助中" not in d["title"]:      # answered since the list was read (open-access ones go in a minute or two)
                        remember(memo, rid)
                        continue
                    if d["supplement"] or not doi:
                        remember(memo, rid)
                        continue
                    try:
                        oa = openalex(doi, sess.ctx)
                    except urllib.error.HTTPError:
                        remember(memo, rid)   # no OpenAlex record for this DOI
                        continue
                    copies = [c for c in oa["copies"] if c[0] in ok]
                    if not copies:
                        remember(memo, rid)
                    else:
                        found += 1
                        say(json.dumps({"req": it["href"], "points": it["points"], "doi": doi, "license": copies[0][0],
                                        "venue": oa["venue"], "title": (oa["title"] or d["title"])[:110]}, ensure_ascii=False))
                except Exception:
                    errs += 1                 # page gone, lookup failed: not remembered, the next scan retries it
                time.sleep(random.uniform(1.0, 2.2))
            if errs:
                say(f"有 {errs} 条没查成（页面或 OpenAlex 出错），下次扫描会重查；候选数偏少时先看这个数。")
            say(f"候选 {found} 条。逐条用 assist --req … --doi … 处理；列表几分钟就变，处理前不用重扫整页。")
        finally:
            page.close()
    return 0


def cmd_assist(a):
    ok = allowed(a)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / (re.sub(r"[^A-Za-z0-9]+", "_", a.doi) + ".pdf")
    with Session(autostart=False) as sess:
        try:
            oa = openalex(a.doi, sess.ctx)
        except Exception as e:
            sys.exit(f"OpenAlex 查不到 {a.doi}：{str(e)[:80]}")
        copies = [c for c in oa["copies"] if c[0] in ok]
        if not copies:
            seen = sorted({c[0] for c in oa["copies"]}) or ["没有带许可证的公开版本"]
            sys.exit(f"不应助：{a.doi} 没有许可范围内的公开版本（OpenAlex：{', '.join(seen)}）。")
        page = sess.ctx.new_page()
        try:
            need_login(page)
            # 1. the request: still open, not a supplement request, and for this DOI
            page.goto(a.req, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            d = page.evaluate(DETAIL_JS)
            if "求助中" not in d["title"]:
                say(f"SKIP  已不是求助中：{d['title'][:50]}")
                return 0
            if d["supplement"]:
                say("SKIP  这条求的是补充材料，不拿正文充数")
                return 0
            if not any(same_doi(a.doi, x) for x in d["dois"]):
                sys.exit(f"详情页上的 DOI {d['dois']} 和 --doi {a.doi} 对不上，不上传。")
            # 2. the open copy
            lic = src = None
            dl = sess.ctx.new_page()
            try:
                for lic, src in copies[:3]:
                    if out.exists():
                        out.unlink()
                    res = nav_fetch(sess, dl, landing(src), out, a.timeout, a.human_wait)
                    if res.get("outcome") == "ok" and not pdf_problem(out):
                        break
                    say(f"  {urllib.parse.urlsplit(src).netloc}：{res.get('outcome')} {str(res.get('detail', ''))[:60]}")
                    lic = src = None
                    time.sleep(random.uniform(3, 6))
            finally:
                dl.close()
            if not src:
                log({"req": a.req, "doi": a.doi, "outcome": "no_file"})
                say("没下到完整的公开版本，这条放弃。")
                return 1
            pages, score, why = article_check(out, oa["title"])
            say(f"  公开版本：{lic}  {src}")
            say(f"  {pages} 页，首页标题吻合 {score:.0%}，{out.stat().st_size // 1024} KB")
            if why:
                log({"req": a.req, "doi": a.doi, "license": lic, "source": src, "outcome": "suspect", "why": why})
                say(f"SUSPECT  {why}，不上传。文件留在 {out}，看过首页再决定。")
                return 1
            # 3. the request again (it may have been answered while the file was downloading), then upload
            page.goto(a.req, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            if "求助中" not in page.title():
                say(f"SKIP  下载期间已被别人应助：{page.title()[:50]}")
                return 0
            if a.dry_run:
                log({"req": a.req, "doi": a.doi, "license": lic, "source": src, "pages": pages, "outcome": "dry_run"})
                say(f"DRY-RUN  会上传 {out.name} 到 {a.req}（没有选文件，没有点上传）")
                return 0
            page.set_input_files("input[type=file]", str(out))
            page.wait_for_timeout(3000)
            page.click("#upload-bth")
            # The success layer may be gone again within seconds (the page refreshes itself), so poll for it
            # and fall back on the page state: an answered request turns 【待确认】.
            toast, done = "", False
            for _ in range(12):
                page.wait_for_timeout(1000)
                try:
                    toast = page.evaluate(TOAST_JS) or toast
                    done = "上传成功" in toast or "待确认" in page.title()
                except Exception:
                    pass                      # navigating
                if done:
                    break
            if not done:
                page.goto(a.req, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                done = "待确认" in page.title()
                toast = toast or ("详情页已是待确认" if done else "")
            log({"req": a.req, "doi": a.doi, "license": lic, "source": src, "pages": pages,
                 "outcome": "uploaded" if done else "unknown", "toast": toast[:120]})
            say(("UPLOADED  " if done else "CHECK  站点没有明确提示成功：") + toast[:80].replace("\n", " "))
            try:
                page.click(".layui-layer-btn0", timeout=2500)
            except Exception:
                pass
            return 0 if done else 1
        finally:
            page.close()


# --------------------------------------------------------------------------- the user's own requests
UPLOADED = f"{SITE}/my/assist-my?status=uploaded"
UPLOADED_JS = r"""() => [...document.querySelectorAll('table tbody tr')].map(tr => {
  const c = tr.querySelector('input[name=assistID]'), d = tr.querySelector('a[href*="/assist/download"]');
  return c ? {req: c.value, download: d ? d.href : '', text: tr.innerText.replace(/\s+/g, ' ').slice(0, 200)} : null;
}).filter(Boolean)"""
LAYER_JS = r"""() => [...document.querySelectorAll('.layui-layer')].map(l => l.innerText.trim()).filter(Boolean).join('\n')"""


def uploaded_rows(page):
    page.goto(UPLOADED, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    return page.evaluate(UPLOADED_JS)


def cmd_request(a):
    """Post one request and bring the answer home. Posting spends the user's points: the caller must have the
    user's consent for this paper (count x points), see SKILL.md section 6. Never adopts: that is `adopt`, after
    `litdl.py ingest` has verified the file."""
    a.doi, a.rid = a.doi.strip(), ""
    earlier = posted_records(a.doi)
    if earlier and not getattr(a, "dry_run", False):
        q = next((d.get("req") for d in reversed(earlier) if d.get("req")), "")
        sys.exit(f"这篇 {earlier[-1].get('time', '')} 已经发过求助（{('求助 id ' + q) if q else '没读到求助 id'}），不重发。"
                 + (f"取文件用 LITA fetch-upload --req {q}。" if q else "先到“我的求助”里核对。"))
    dest = HOME / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            bal = need_login(page)
            if bal < 10:
                sys.exit(f"积分不够：余额 {bal}，发一篇要 10。")
            rows = uploaded_rows(page)
            if rows:
                sys.exit("有求助已经收到文件还没处理，平台不让发下一篇。先核验、采纳或驳回：\n  "
                         + "\n  ".join(f"{r['req']}  {r['text'][:90]}" for r in rows))
            # 1. let the site look the DOI up, and compare its card with the manifest
            page.goto(f"{SITE}/assist/create", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            if not page.query_selector("#onekey"):
                sys.exit(f"发布页不是平常的样子（{page.title()[:40]}），没有发。用 litbrowser.py text 看一下。")
            page.fill("#onekey", a.doi)
            page.click("button.onekey-search")
            card = ""
            for _ in range(25):
                page.wait_for_timeout(1000)
                card = page.evaluate(LAYER_JS)
                if "信息正确" in card or "信息有误" in card:
                    break
            m_doi = re.search(r"DOI\s+(\S+)", card)
            m_title = re.search(r"标题\s+(.+)", card)
            want, got = litdl.norm(a.title), litdl.norm(m_title.group(1) if m_title else "")
            words = [w for w in want.split() if len(w) > 3]
            score = sum(w in got.split() for w in words) / len(words) if words else 0.0
            if not (m_doi and same_doi(m_doi.group(1), a.doi) and score >= 0.9):
                log({"cmd": "request", "doi": a.doi, "outcome": "card_mismatch", "card": card[:300]})
                say(f"SUSPECT  站点提取出的信息和清单对不上（题名吻合 {score:.0%}），没有发布。卡片内容：\n{card[:400]}")
                return 1
            say("  站点提取的信息：" + re.sub(r"\s*\n\s*", " | ", card.split("信息正确")[0].strip())[:300])
            if getattr(a, "dry_run", False):
                say(f"DRY-RUN  卡片和清单对得上（题名吻合 {score:.0%}），余额 {bal}。没有点发布，没有扣积分。")
                return 0
            # 2. publish (default reward: 10 points, the minimum)
            page.click(".layui-layer-btn0")
            ok, rid = False, ""
            for _ in range(15):
                page.wait_for_timeout(1000)
                if "求助发布成功" in page.evaluate(LAYER_JS):
                    ok = True                 # the dialog links to the new request: that id tells our row from older ones
                    rid = page.evaluate("""() => { const l = document.querySelector('.layui-layer a[href*="assist/detail"]');
                                                   return l ? l.href.split('id=').pop() : ''; }""")
                    break
            log({"cmd": "request", "doi": a.doi, "req": rid, "outcome": "posted" if ok else "post_unclear"})
            if not ok:
                say("CHECK  点了发布，没读到“求助发布成功”。先到“我的求助”里看有没有这一条，不要重发。")
                return 1
            if not rid:                       # the dialog's button is not a plain link (2026-11-21): the points history
                try:                          # lists the new request first, "发布求助 (id=…)"
                    page.goto(f"{SITE}/my/point", wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1200)
                    m = re.search(r"发布求助\s*\(id=([A-Za-z0-9]+)\)", page.evaluate("() => document.body.innerText"))
                    rid = m.group(1) if m else ""
                except Exception:
                    pass
            a.rid = rid                       # request-batch reads it back
            say(f"  已发布（-10 积分）：{a.doi}  req={rid or '?'}")
            # 3. wait for a file on THIS request (an older request of the user's may get its file meanwhile)
            def mine(r):
                return r["req"] == rid if rid else litdl.norm(a.title)[:40] in litdl.norm(r["text"])
            row, t0 = None, time.time()
            while time.time() - t0 < a.wait:
                rows = uploaded_rows(page)
                row = next((r for r in rows if r["download"] and mine(r)), None)
                if row:
                    break
                time.sleep(20)
            if not row:
                say(f"WAITING  {a.wait} 秒内还没有人上传。求助留在站上（req={rid or '?'}），之后用 fetch-upload --req 取。")
                return 2
            a.rid = row["req"]
            return take_upload(sess, row, a.timeout, dest)
        finally:
            page.close()


def take_upload(sess, row, timeout, dest):
    """Download the file somebody uploaded to one of the user's requests (free line) into ~/.litdl/downloads."""
    say(f"  有人上传了：req={row['req']}  {row['text'][:110]}")
    # 4. download on the free line
    got_dl = []
    dl = sess.ctx.new_page()
    dl.on("download", lambda d: got_dl.append(d))
    try:
        dl.goto(row["download"], wait_until="domcontentloaded", timeout=45000)
        t0 = time.time()
        while not got_dl and time.time() - t0 < timeout:
            dl.wait_for_timeout(3000)
            try:                      # "下载持续较慢" -> keep the free line; never the paid fast lane
                btn = dl.query_selector(".layui-layer-btn a:has-text('继续当前下载')")
                if btn:
                    btn.click()
            except Exception:
                pass
        if not got_dl:
            say(f"TIMEOUT  {timeout} 秒内下载没有开始或没有完成。下载页：{row['download']}  req={row['req']}")
            return 3
        d = got_dl[0]
        out = dest / re.sub(r'[\\/:*?"<>|]', "_", d.suggested_filename or (row["req"] + "(科研通-ablesci.com).pdf"))
        d.save_as(str(out))
    finally:
        dl.close()
    bad = pdf_problem(out)
    log({"cmd": "request", "req": row["req"], "file": out.name, "outcome": "downloaded", "problem": bad or ""})
    say(f"DOWNLOADED  req={row['req']}  {out}  {out.stat().st_size // 1024} KB" + (f"  文件有问题：{bad}" if bad else ""))
    say("  下一步：litdl.py ingest 核验入库；通过了再 adopt --req " + row["req"] + "，不对就到详情页驳回。")
    return 0


def cmd_fetch_upload(a):
    """For a request that got its file after `request` stopped waiting: download it, same as step 4 there."""
    rid = a.req.strip().split("id=")[-1]
    dest = HOME / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            need_login(page)
            rows = uploaded_rows(page)
            row = next((r for r in rows if r["req"] == rid and r["download"]), None)
            if not row:
                say(f"WAITING  {rid} 还没有人上传（“待确认”里现有：{[r['req'] for r in rows]}）。")
                return 2
            return take_upload(sess, row, a.timeout, dest)
        finally:
            page.close()


def cmd_adopt(a):
    rid = a.req.strip().split("id=")[-1]
    with Session(autostart=False) as sess:
        page = sess.ctx.new_page()
        try:
            need_login(page)
            rows = uploaded_rows(page)
            if rid not in [r["req"] for r in rows]:
                sys.exit(f"“待确认”里没有 {rid}（现有：{[r['req'] for r in rows]}）。")
            page.evaluate("id => document.querySelector(`input[name=assistID][value=\"${id}\"]`).nextElementSibling.click()", rid)
            checked = page.evaluate("() => [...document.querySelectorAll('input[name=assistID]')].filter(c => c.checked).map(c => c.value)")
            if checked != [rid]:
                sys.exit(f"勾选的不是只有 {rid}（{checked}），没有采纳。")
            page.click("#accept-assist-batch")
            page.wait_for_timeout(1500)
            if "确认采纳" not in page.evaluate(LAYER_JS):
                sys.exit("没有弹出采纳确认框，没有采纳。")
            page.click(".layui-layer-btn0")
            page.wait_for_timeout(3000)
            left = [r["req"] for r in uploaded_rows(page)]
            log({"cmd": "adopt", "req": rid, "outcome": "adopted" if rid not in left else "unclear"})
            if rid in left:
                say(f"CHECK  点了确定，但 {rid} 还在“待确认”里。")
                return 1
            say(f"ADOPTED  {rid}")
            return 0
        finally:
            page.close()


# --------------------------------------------------------------------------- a batch of requests, one row at a time
def _litdl(*args):
    """litdl.py in its own process (it stays the only writer of the manifest) -> its output"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-P", str(HERE / "litdl.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


def _step(fn, ns):
    """One of the commands above, in this process -> (return code, the message of a sys.exit if it refused).
    Any other error ends up here too (a page that timed out while waiting for an upload, say): the batch must get
    the chance to write down that a request may already be on the site (review 2026-11-22)."""
    try:
        return fn(ns), ""
    except SystemExit as e:
        return 9, str(e.code)
    except Exception as e:
        return 8, f"出错：{str(e).splitlines()[0][:160] if str(e) else type(e).__name__}"


def posted_records(doi, since=""):
    """Log records saying this DOI was posted, or may have been (clicked publish, no confirmation), at or after
    `since` ('YYYY-mm-dd HH:MM:SS'). The log is written right after the publish click, before any waiting, so it
    is what still knows about a request when the ledger does not."""
    out = []
    try:
        lines = (OUT / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if (d.get("cmd") == "request" and d.get("outcome") in ("posted", "post_unclear")
                and same_doi(d.get("doi") or "", doi) and (d.get("time") or "") >= since):
            out.append(d)
    return out


def cmd_request_batch(a):
    """request -> ingest -> mark -> adopt for each row, in that order, one row at a time (the 2026-11-21 loop).
    What it guards, so that the caller cannot get these wrong:
      - only rows whose ledger has an entry for every database that can hold the paper (litdl.required);
      - never more new requests than --budget pays for (the points the user agreed to for this batch);
      - a row that already has a request on the site is not posted again, only checked for a file;
      - adopt only after ingest printed FILED for exactly this row;
      - the first step that is not clean ends the batch; nothing is posted after it."""
    today = time.strftime("%Y-%m-%d")
    rows = {r["id"]: r for r in litdl.load(a.manifest)}
    post, pending, bad = [], [], []
    ids = list(dict.fromkeys(int(x) for x in a.ids.split(",") if x.strip()))    # "3,3" is one row, posted once
    for i in ids:
        r = rows.get(i)
        if r is None:
            bad.append(f"#{i} 不在清单里")
            continue
        tried = r.get("tried") or {}
        need = litdl.required(r)                  # the databases that can hold this paper (litdl.py locate)
        lack = [c for c in need if c not in tried]
        m = re.search(r"求助 id\s*([A-Za-z0-9]+)", tried.get("ablesci", ""))
        if r["status"] in ("done", "have"):
            bad.append(f"#{i} 已经有了（{litdl.STATUS_CN.get(r['status'], r['status'])}）")
        elif m:                                   # already posted: only look for a file, whatever the ledger says now
            pending.append((i, m.group(1)))
        elif lack or not need:
            bad.append(f"#{i} 还有库没试：{'、'.join(lack) or '没有可判断的库'}")
        elif tried.get("ablesci"):
            bad.append(f"#{i} 的账本里已有科研通记录（{tried['ablesci'][:50]}），不重发")
        elif not r.get("doi"):
            bad.append(f"#{i} 没有 DOI")
        elif posted_records(r["doi"]):
            q = next((d.get("req") for d in reversed(posted_records(r["doi"])) if d.get("req")), "")
            bad.append(f"#{i} 科研通的流水里有这篇发布过的记录（{('求助 id ' + q) if q else '没读到求助 id'}），账本里却没有。"
                       "先到“我的求助”核对，再用 LITDL mark 记上，不重发")
        else:
            post.append(i)
    if bad:
        sys.exit("这一批不能发，一篇都没有发：\n  " + "\n  ".join(bad))
    cost = 10 * len(post)
    say(f"要发 {len(post)} 篇（{cost} 积分）：{post or '无'}；已发过、只查有没有文件的：{[f'#{i}:{q}' for i, q in pending] or '无'}")
    if cost > a.budget:
        sys.exit(f"要花 {cost} 积分，超过 --budget {a.budget}（用户同意的数）。一篇都没有发。")
    if a.dry_run:
        say("DRY-RUN  只列计划，没有打开科研通。")
        return 0

    def finish(i, rid):                       # the file is in ~/.litdl/downloads
        out = _litdl("ingest", a.manifest, "--src", str(HOME / "downloads"), "--pattern", "*ablesci.com*.pdf",
                     "--move", "--dest", a.dest)
        filed = next((ln for ln in out.splitlines() if re.match(rf"FILED\s+#{i}\s", ln)), "")
        if not filed:
            say(f"STOP  ingest 没有把 #{i} 入库，req={rid} 没有采纳也没有驳回。文件留在 {HOME / 'downloads'}，"
                "先看首页文字和 PDF 里的 DOI。ingest 的输出：")
            say("  " + "\n  ".join([ln for ln in out.splitlines() if ln.strip()][:6]))
            return False
        say("  " + filed[:220])
        _litdl("mark", a.manifest, "--ids", str(i), "--tried", f"ablesci=已应助（{today}）")
        rc, msg = _step(cmd_adopt, argparse.Namespace(req=rid))
        if rc != 0:
            say(f"STOP  #{i} 已入库，采纳这一步不明确（req={rid}）。{msg}")
            return False
        return True

    def sweep():                              # files that arrived for requests posted earlier
        nonlocal pending
        for i, rid in list(pending):
            rc, msg = _step(cmd_fetch_upload, argparse.Namespace(req=rid, timeout=a.timeout))
            if rc == 2:                       # still nobody
                continue
            if rc != 0:
                say(f"STOP  取 #{i} 的文件这一步不明确（req={rid}）。{msg}")
                return False
            pending = [p for p in pending if p != (i, rid)]
            if not finish(i, rid):
                return False
        return True

    ok = True
    for i in post:
        if not sweep():
            ok = False
            break
        r = rows[i]
        title = r["title"] + ((": " + r["subtitle"]) if r.get("subtitle") else "")
        say(f"=== #{i}  {r['doi']}  {title[:100]}  ({time.strftime('%H:%M:%S')})")
        ns = argparse.Namespace(doi=r["doi"], title=title, wait=a.wait, timeout=a.timeout, dry_run=False, rid="")
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        rc, msg = _step(cmd_request, ns)
        if rc == 0 and ns.rid:
            if not finish(i, ns.rid):
                ok = False
                break
        elif rc == 2 and ns.rid:              # posted, nobody has uploaded yet: the ledger keeps the request id
            _litdl("mark", a.manifest, "--ids", str(i), "--tried", f"ablesci=已发求助，还没人应助（{today}，求助 id {ns.rid}）")
            pending.append((i, ns.rid))
        else:
            # anything unclear after the publish click: write down that a request may be on the site before stopping,
            # or the next run would post this row again (review 2026-11-22)
            posted = posted_records(r["doi"], since=started)
            if posted or rc in (0, 2):
                rid = ns.rid or next((d.get("req") for d in reversed(posted) if d.get("req")), "")
                note = f"求助 id {rid}" if rid else "求助 id 没读到，到“我的求助”里查"
                _litdl("mark", a.manifest, "--ids", str(i), "--tried", f"ablesci=已发求助，后面一步不明确（{today}，{note}）")
                say(f"STOP  #{i} 已经发出去了，后面一步不明确，账本里记下了，后面的没有发。{msg}")
            else:
                say(f"STOP  #{i} 这一步不明确，后面的没有发。{msg}")
            ok = False
            break
        time.sleep(random.uniform(6, 10))
    if ok:
        ok = sweep()
    say("还在等人应助的：" + (" ".join(f"#{i}:{q}" for i, q in pending) or "无"))
    _step(cmd_points, argparse.Namespace(last=6))
    say("\n".join([ln for ln in _litdl("missing", a.manifest).splitlines() if ln.strip()][:2]))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("points", help="balance and recent history")
    p.add_argument("--last", type=int, default=8)
    p = sp.add_parser("signin", help="the daily sign-in (+10 or +20 points); does nothing when today's is already done")
    p.add_argument("--dry-run", action="store_true", help="say whether today's sign-in is still open, do not click")
    p = sp.add_parser("scan", help="list open requests that an openly licensed copy can answer (read-only)")
    p.add_argument("--pages", type=int, default=3, help="list pages to read")
    p.add_argument("--max", type=int, default=8, help="stop after this many candidates")
    p.add_argument("--visit", type=int, default=40, help="look at no more than this many request pages")
    p.add_argument("--by-points", action="store_true",
                   help="highest reward first (default: newest first - open-access requests are answered within minutes)")
    p.add_argument("--skip", default="", help="comma-separated request ids already handled")
    p.add_argument("--rescan", action="store_true",
                   help="also reopen requests an earlier scan found no eligible copy for (use after changing --allow-nc)")
    p.add_argument("--allow-nc", action="store_true", help="also accept CC BY-NC / -ND licences")
    p = sp.add_parser("assist", help="answer one request with the OA copy of its DOI")
    p.add_argument("--req", required=True, help="the request page: https://www.ablesci.com/assist/detail?id=…")
    p.add_argument("--doi", required=True)
    p.add_argument("--allow-nc", action="store_true", help="also accept CC BY-NC / -ND licences")
    p.add_argument("--dry-run", action="store_true", help="download and verify, do not upload")
    p.add_argument("--timeout", type=int, default=200, help="seconds for the PDF")
    p.add_argument("--human-wait", type=int, default=45, help="seconds to wait at a verification page")
    p = sp.add_parser("request", help="post ONE request (spends 10 of the user's points), wait for a file, download it")
    p.add_argument("--doi", required=True)
    p.add_argument("--title", required=True, help="the title in the manifest; the site's metadata card must match it")
    p.add_argument("--wait", type=int, default=600, help="seconds to wait for somebody to upload a file")
    p.add_argument("--timeout", type=int, default=1800, help="seconds for the download (the free line is slow)")
    p.add_argument("--dry-run", action="store_true",
                   help="go as far as the site's metadata card and compare it with --title; do not publish, spend nothing")
    p = sp.add_parser("request-batch",
                      help="request -> ingest -> mark -> adopt for several manifest rows, one at a time; stops at anything unclear")
    p.add_argument("manifest")
    p.add_argument("--ids", required=True, help="comma-separated manifest row ids, the ones the user agreed to")
    p.add_argument("--dest", required=True, help="library root, as for litdl.py ingest")
    p.add_argument("--budget", type=int, required=True,
                   help="points the user agreed to spend on this batch (10 per new request); more than that is refused")
    p.add_argument("--wait", type=int, default=420, help="seconds to wait for an upload before moving on to the next row")
    p.add_argument("--timeout", type=int, default=1800, help="seconds for each download")
    p.add_argument("--dry-run", action="store_true", help="check the rows and the budget, print the plan, touch nothing")
    p = sp.add_parser("fetch-upload", help="download the file a still-open request of the user's has received since")
    p.add_argument("--req", required=True, help="request id")
    p.add_argument("--timeout", type=int, default=1800)
    p = sp.add_parser("adopt", help="accept the file uploaded to one of the user's requests (after ingest verified it)")
    p.add_argument("--req", required=True, help="request id, as printed by `request`")
    a = ap.parse_args()
    if a.cmd == "assist":
        a.req, a.doi = a.req.strip(), a.doi.strip()      # a DOI piped from another program may carry a trailing CR
    if a.cmd == "assist" and not a.req.startswith(f"{SITE}/assist/detail"):
        sys.exit("--req 要写完整的求助详情页网址（https://www.ablesci.com/assist/detail?id=…）")
    return {"points": cmd_points, "signin": cmd_signin, "scan": cmd_scan, "assist": cmd_assist,
            "request": cmd_request, "request-batch": cmd_request_batch,
            "fetch-upload": cmd_fetch_upload, "adopt": cmd_adopt}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
