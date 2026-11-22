#!/usr/bin/env python3
"""litbrowser - the browser channel of litdl, driven by Playwright.

A dedicated Chrome (own profile folder, started as a normal process with a local debugging port) stays open
between commands; every command attaches to it over CDP, does its work and detaches. The user logs in to the
school WebVPN (and other sites) once in that window; nothing here types passwords or reads SMS codes.

Human-verification pages are got past "hands off" (get_past / handsoff_fetch): the session lets go of the
browser, the same address is opened again with nothing attached, a known checkbox or Sci-Hub's own button is
clicked with the system mouse (a click / slider puzzle goes to the vision model when one is configured, see
captcha_ai.py), and the user is asked when that does not get through; afterwards the session attaches again and
the recipe goes on in that tab. Verifications do not pass while automation is attached (ScienceDirect,
2026-11-22), so this is the rule for all of them (user, the same day). LITDL_HANDSOFF=0 goes back to the older
attached attempt and wait. `solve` makes one attempt on one tab by hand; `look` / `press` read and click a tab
with nothing attached, for when a Claude session acts as the vision model.

Bulk work:
  open      start the dedicated Chrome if it is not running
  status    is Chrome up, is the WebVPN session valid, which tabs are open
  login     open a login page, bring it to the front, wait until the hard login signal appears
  portal    after the login: read the WebVPN portal's own page and list the databases it offers (entries, host
            forms, EBSCO customer id). Finds nothing -> asks where the list is and reads the page the user names
  fetch     download manifest rows one at a time (EBSCO / Wiley / Springer+IEEE / Elsevier / T&F / Sci-Hub / public
            copies). --via auto sends each row to the next database that can hold it and has no answer in the
            row's ledger yet; --detach runs it as a process of its own for rounds longer than a few minutes
  get       download one URL (a PDF or a landing page) for one manifest id
  progress  what a running or finished fetch has done so far (reads <stage>/progress.json)
  wait      block until a fetch ends or stops at a verification page (at most --timeout seconds)
  pause     ask a running fetch to stop after the paper in flight
  quit      close the dedicated Chrome (the WebVPN session is lost)

Step-by-step primitives for pages that have no recipe yet:
  tabs / goto / eval / text / click / fill / shot / front / close
  widgets  list known captcha iframes (incl. closed shadow roots, via CDP) with their screen boxes
  clickat  click one CSS-pixel point
  drag     press, move along an eased arc, release
These attach to the browser (tabs does not). On a verification page use only the ones that attach nothing:
  look     title, verification controls with their click points, text; --out a screenshot
  press    click / drag / hover one CSS-pixel point with the system mouse

When a Claude session is doing the work, it can act as the vision model: look --out -> read the PNG ->
press. No separate model key is needed for that; the key is only for unattended --detach runs and for
`solve` when nobody is watching.

Run with `python -X utf8 -P`. Needs the `playwright` package; uses the installed Chrome, no browser download.
"""
import argparse
import base64
import ctypes
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import litdl  # noqa: E402  (Pacer, manifest helpers, title normalisation)
import captcha_ai  # noqa: E402  (optional vision-model pass over click / slider verification)

HOME = Path(os.environ.get("LITDL_HOME") or (Path.home() / ".litdl"))
PROFILE = HOME / "chrome-profile"
PORT = int(os.environ.get("LITDL_CDP_PORT") or 9333)
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]
THROTTLE = {403, 429, 503}
# The dedicated Chrome is started with --no-proxy-server: it never goes through the Windows system proxy (Clash),
# whether or not one is switched on (user, 2026-11-21). The school WebVPN works from any network this way, and
# publisher / repository sites see a home or campus address instead of the proxy's foreign exit, which is what
# their verification pages react to. --use-proxy (or LITDL_CHROME_PROXY=1) is the way back, only on request.
CHROME_DIRECT = os.environ.get("LITDL_CHROME_PROXY", "").strip().lower() not in ("1", "true", "yes")
MODE_FILE = HOME / "chrome-mode.json"
# Sci-Hub mirrors, tried in this order (LITDL_SCIHUB: comma-separated hosts)
SCIHUB = [h.strip() for h in (os.environ.get("LITDL_SCIHUB") or "sci-hub.ru,sci-hub.st,sci-hub.se").split(",") if h.strip()]

# base gap (s) between two papers of one channel; the Pacer adds jitter, batch rests and back-off on top
GAPS = {"ebsco": 20, "wiley": 18, "direct": 15, "elsevier": 28, "public": 12, "tandf": 15, "pubdoi": 15, "scihub": 12,
        "vpnpub": 15}
# how long one PDF may take (s). EBSCO through WebVPN needs 40-180 s as a rule, large scans 4-5 min.
FETCH_TIMEOUT = {"ebsco": 480, "wiley": 240, "direct": 240, "elsevier": 240, "public": 240,
                 "tandf": 240, "pubdoi": 240, "scihub": 240, "vpnpub": 240}


# --------------------------------------------------------------------------- small helpers
def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def say(*a):
    print(*a, flush=True)


def pid_alive(pid):
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259                                           # STILL_ACTIVE


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def cdp_version():
    """-> dict from /json/version when the dedicated Chrome is listening, else None. Never goes through a proxy."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def find_chrome(explicit=None):
    for c in ([explicit] if explicit else []) + CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    sys.exit("没找到 Chrome/Edge，可用 --exe 指定浏览器路径")


def prepare_profile():
    """First start only: PDFs download instead of opening in the viewer, no save-as dialog, session restored on restart."""
    pref = PROFILE / "Default" / "Preferences"
    if pref.exists():
        return
    pref.parent.mkdir(parents=True, exist_ok=True)
    (HOME / "downloads").mkdir(parents=True, exist_ok=True)
    write_json(pref, {
        "plugins": {"always_open_pdf_externally": True},
        "download": {"prompt_for_download": False, "default_directory": str(HOME / "downloads"), "directory_upgrade": True},
        "session": {"restore_on_startup": 1},
        "profile": {"name": "litdl 文献下载"},
    })


def start_chrome(exe=None, wait=25):
    if cdp_version():
        return False
    prepare_profile()
    args = [find_chrome(exe), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            # timers in background tabs keep running, so a download in a hidden tab is not slowed down
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "about:blank"]
    if CHROME_DIRECT:
        args.insert(1, "--no-proxy-server")          # direct connection, never the system proxy (Clash)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        if cdp_version():
            write_json(MODE_FILE, {"direct": CHROME_DIRECT, "pid": p.pid, "started": now()})
            return True
        time.sleep(0.5)
    sys.exit(f"Chrome 已启动但 {wait} 秒内调试端口 {PORT} 没有响应；看是否有另一个 Chrome 占用了同一个 profile：{PROFILE}")


def chrome_direct(sess):
    """Was the running dedicated Chrome started with --no-proxy-server? -> True / False / None (cannot tell).
    Chrome does not report its own command line over CDP unless --enable-automation is set, so: the note
    start_chrome() left, if it is about this very process; else, on Windows, the process's command line."""
    try:
        s = sess.browser.new_browser_cdp_session()
        pid = next(p["id"] for p in s.send("SystemInfo.getProcessInfo")["processInfo"] if p["type"] == "browser")
    except Exception:
        return None
    note = read_json(MODE_FILE) or {}
    if note.get("pid") == pid:
        return bool(note.get("direct"))
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                           capture_output=True, text=True, timeout=20)
        if "--remote-debugging-port" not in r.stdout:
            return None
        direct = "--no-proxy-server" in r.stdout
        write_json(MODE_FILE, {"direct": direct, "pid": pid, "started": "?"})
        return direct
    except Exception:
        return None


class Session:
    """Attach to the dedicated Chrome; detach on exit (the browser and its tabs stay)."""

    def __init__(self, autostart=True, exe=None):
        self.autostart, self.exe = autostart, exe
        self.pw = self.browser = self.ctx = None
        self.generation = 0                   # goes up each time the session lets go of the browser (see release)

    def __enter__(self):
        for k in ("NO_PROXY", "no_proxy"):
            v = os.environ.get(k, "")
            if "127.0.0.1" not in v:
                os.environ[k] = (v + "," if v else "") + "127.0.0.1,localhost"
        if not cdp_version():
            if not self.autostart:
                sys.exit("专用 Chrome 没在运行，先跑 litbrowser open")
            used_before = (PROFILE / "Default").exists()
            if start_chrome(self.exe) and used_before:
                # it was up before and is gone (closed by the user, or crashed on a page): session cookies went with it
                say("注意：专用 Chrome 没在运行，已重新启动。WebVPN 的登录随上一个进程没了，要用户重新登录（status 可查）。")
        self._connect()
        return self

    def _connect(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("缺少 playwright：pip install playwright（不需要 playwright install，用本机 Chrome）")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}", timeout=20000)
        except Exception:
            try:                                  # a second start in this thread would fail while this one runs
                self.pw.stop()
            except Exception:
                pass
            self.pw = self.browser = self.ctx = None
            raise
        self.ctx = self.browser.contexts[0]
        self.ctx.set_default_timeout(30000)

    def release(self):
        """Let go of the browser: afterwards no DevTools client of ours is attached to any tab.

        Page objects taken before this are dead after it; `reattach` connects again and bumps `generation`,
        so a caller holding a page can tell it needs a new one."""
        try:
            if self.browser:
                self.browser.close()          # over CDP this only disconnects
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = self.ctx = None
        self.generation += 1

    def reattach(self):
        if self.ctx is None:
            self._connect()

    def __exit__(self, *exc):
        try:
            if self.browser:
                self.browser.close()          # over CDP this only disconnects
        except Exception:
            pass                              # the browser is already gone
        finally:
            if self.pw:
                self.pw.stop()

    # -- tabs
    def target_id(self, page):
        """The tab's full DevTools target id (what /json/list calls `id`), or ''."""
        try:
            s = self.ctx.new_cdp_session(page)
            try:
                return s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            finally:
                s.detach()
        except Exception:
            return ""

    def tab_id(self, page):
        return (self.target_id(page) or "??????")[:6]

    def page_by_id(self, tid):
        """The page object of a tab known by its target id (e.g. one opened hands off before attaching), or None."""
        for p in self.ctx.pages:
            if self.target_id(p) == tid:
                return p
        return None

    def tabs(self):
        return [(self.tab_id(p), p) for p in self.ctx.pages]

    def find(self, key):
        """tab by id prefix, or by a piece of its URL/title; 'new' opens one."""
        if key in (None, "", "new"):
            return self.ctx.new_page()
        hits = [p for tid, p in self.tabs() if tid.lower().startswith(key.lower())]
        if not hits:
            hits = [p for p in self.ctx.pages if key.lower() in p.url.lower() or key.lower() in safe_title(p).lower()]
        if len(hits) != 1:
            sys.exit(f"--tab {key!r} 匹配到 {len(hits)} 个标签页；先跑 tabs 看清楚")
        return hits[0]


def safe_title(page):
    try:
        return page.title()
    except Exception:
        return ""


def tidy(sess, *keep):
    """Close blank tabs left behind by downloads that opened in a new tab. Always leaves one tab: no tab, no window."""
    for p in list(sess.ctx.pages):
        if len(sess.ctx.pages) <= 1:
            break
        if p not in keep and not p.is_closed() and p.url in ("about:blank", "", "chrome://newtab/"):
            try:
                p.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- page state: verification / block / login
JS_STATE = r"""() => {
  const t = document.title || '';
  const b = document.body ? document.body.innerText.slice(0, 3000) : '';
  // a solved widget leaves its iframe on the page; the token field is the hard success signal
  const solved = ['input[name="cf-turnstile-response"]', 'textarea[name="g-recaptcha-response"]',
      'textarea[name="h-captcha-response"]', 'input[name="h-captcha-response"]'].some(s => {
    const e = document.querySelector(s);
    return e && String(e.value || '').length > 20;
  });
  // a visible captcha iframe counts; reCAPTCHA's invisible-mode badge (size=invisible / badge=) does not
  const visibleWidget = s => Array.from(document.querySelectorAll(s)).some(el => {
    const r = el.getBoundingClientRect();
    const src = el.getAttribute('src') || '';
    return r.width > 20 && r.height > 18 && !src.includes('size=invisible') && !src.includes('badge=');
  });
  const widget = !!document.querySelector('#challenge-form, #cf-challenge-running, #challenge-stage, ' +
      'input[name="cf-turnstile-response"], .geetest_holder, #px-captcha') ||
      visibleWidget('iframe[src*="challenges.cloudflare.com"]') ||
      visibleWidget('iframe[src*="recaptcha"]') ||
      visibleWidget('iframe[src*="hcaptcha"]');
  const words = /just a moment|请稍候|verify you are human|确认您是真人|are you a robot|你是机器人|checking your browser|needs to review the security|人机验证|安全验证|滑动验证/i.test(t + ' ' + b.slice(0, 600));
  // Cloudflare's interstitial says "验证成功。正在等待…" after the challenge but keeps the same title
  const success = /验证成功|验证通过|verification (was )?successful/i.test(b.slice(0, 800) + ' ' + t);
  const gate = location.hostname.endsWith('ssrn.com') && /download without registration/i.test(b);
  return {
    title: t.slice(0, 120), url: location.href, len: b.length,
    challenge: !solved && !success && !gate && (widget || (words && b.length < 1500)),
    blocked: /there was a problem providing the content you requested|access denied|unusual traffic|too many requests|rate limit/i.test(b.slice(0, 1500)),
    denied: /access denied|restricted access|access (is |to this (file|item) is )?restricted|under embargo|embargoed until|not authori[sz]ed to (view|access)/i.test(b.slice(0, 1500)),
    // SSRN prints this when the author has not put the paper up (2026-11-22, #119); its citation_pdf_url still
    // points at Delivery.cfm, which only leads back to the abstract
    nodl: location.hostname.endsWith('ssrn.com') && /not available for download/i.test(b),
    // SSRN's "sign up first" page (Data_Integrity_Notice.cfm, title "SSRN Alert", 2026-11-22, #157). The Cloudflare box
    // on it belongs to the sign-up form; the way on is its own "Download without registration" button
    ssrngate: gate,
    signin: /\/users\/sign_in|\/login\b|\/site\/login/.test(location.pathname),
    isPdf: document.contentType === 'application/pdf'
  };
}"""


def page_state(page):
    for _ in range(3):
        try:
            return page.evaluate(JS_STATE)
        except Exception:                     # navigating: the context was destroyed, look again in a moment
            try:
                page.wait_for_timeout(800)
            except Exception:
                break
    return {"title": "", "url": page.url, "len": 0, "challenge": False, "blocked": False, "denied": False,
            "nodl": False, "ssrngate": False, "signin": False, "isPdf": False}


def wait_human(page, why, seconds, prog=None):
    """A verification page is showing, and the hands-off path is switched off (LITDL_HANDSOFF=0) or cannot run (no
    websockets package): the older way, on the attached page (see get_past for the default).

    If a vision model is configured, try one bounded click / slider pass first (captcha_ai.solve).
    Everything else - no model, an unrecognised widget, an attempt that failed - falls through to the
    original behaviour: bring the page to the front, tell the user, poll quietly. Account data is
    never typed, read or clicked here."""
    try:
        if captcha_ai.solve(page, why, state_fn=page_state, home=HOME, log=say):
            if prog:
                prog.need_user(None, None)
            return True
    except Exception as e:
        say(f"       AI 过验证出错，交回给用户：{str(e)[:120]}")
    try:
        page.bring_to_front()
    except Exception:
        pass
    say(f"NEED_USER  {why}  标签页：{safe_title(page)!r} {page.url[:90]}  （等 {seconds} 秒）")
    if prog:
        prog.need_user(why, page.url)
    t0, clear = time.time(), 0
    while time.time() - t0 < seconds:
        try:
            page.wait_for_timeout(3000)
        except Exception:
            return False
        # a verification page that reloads itself looks "clear" for a moment: ask for three clear looks in a row
        clear = 0 if page_state(page)["challenge"] else clear + 1
        if clear >= 3:
            if prog:
                prog.need_user(None, None)
            say("       验证已通过，继续")
            return True
    return False


def vpn_logged_in(sess, vpn, marker="sign_out"):
    """Hard signal, read-only; never logs in or types anything.

    The portal redirects a *new* tab to /users/sign_in even while the proxy session is valid, so
    probing in a fresh tab alone gives a false "not logged in". Check existing tabs first:
      1. a portal tab showing the sign-out marker;
      2. any tab on a WebVPN-proxied host that is not sitting on a sign-in page.
    Only then fall back to a throw-away probe tab, which is closed afterwards."""
    try:
        for p in sess.ctx.pages:
            u = p.url
            if not u.startswith("http"):
                continue
            host = urllib.parse.urlsplit(u).hostname or ""
            if host == vpn or host.endswith("." + vpn):
                if "/users/sign_in" not in u and "/site/login" not in u:
                    if host == vpn and marker in p.content():
                        return True
                    if host != vpn:                  # a proxied publisher/database page loaded: session works
                        return True
    except Exception:
        pass
    page = sess.ctx.new_page()
    try:
        page.goto(f"https://{vpn}/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        return "/users/sign_in" not in page.url and marker in page.content()
    except Exception as e:
        say(f"       门户打不开：{str(e)[:120]}")
        return False
    finally:
        try:
            page.close()                             # do not leave probe tabs behind
        except Exception:
            pass


# --------------------------------------------------------------------------- getting bytes out of the browser
JS_FETCH = r"""async ([url, timeoutMs]) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {credentials: 'include', signal: ctl.signal});
    const buf = new Uint8Array(await r.arrayBuffer());
    window.__litdl = buf;
    const dec = a => Array.from(a).map(c => String.fromCharCode(c)).join('');
    return {status: r.status, ct: r.headers.get('content-type') || '', size: buf.length,
            head: dec(buf.slice(0, 5)), eof: dec(buf.slice(-2048)).includes('%%EOF'), final: r.url};
  } catch (e) {
    return {error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}"""
JS_CHUNK = r"""async ([a, b]) => await new Promise(res => {
  const fr = new FileReader();
  fr.onload = () => res(fr.result.slice(fr.result.indexOf(',') + 1));
  fr.readAsDataURL(new Blob([window.__litdl.slice(a, b)]));
})"""


def pdf_problem(path):
    """'' when the file is a complete PDF, else a short reason. A cut-off download keeps the %PDF head but loses %%EOF."""
    p = Path(path)
    with p.open("rb") as f:
        data_head = f.read(5)
    if data_head != b"%PDF-":
        return "不是 PDF"
    with p.open("rb") as f:
        f.seek(max(0, p.stat().st_size - 4096))
        if b"%%EOF" not in f.read():
            return "被截断（缺 %%EOF）"
    try:
        import fitz
        d = fitz.open(p)
        n = d.page_count
        d.close()
        if n < 1:
            return "0 页"
    except ImportError:
        pass
    except Exception as e:
        return f"打不开（{str(e)[:40]}）"
    return ""


def inpage_fetch(page, url, out, timeout):
    """fetch() inside the page (same origin, the page's own session) and move the bytes to `out`.
    -> dict(outcome=ok|throttle|not_pdf|truncated|error, status, size, detail)"""
    try:
        meta = page.evaluate(JS_FETCH, [url, int(timeout * 1000)])
    except Exception as e:
        return {"outcome": "error", "detail": f"页面在取文件时跳转或关闭：{str(e)[:100]}"}
    if meta.get("error"):
        return {"outcome": "error", "detail": meta["error"][:140]}
    res = {"status": meta["status"], "size": meta["size"]}
    if meta["status"] in THROTTLE:
        return {**res, "outcome": "throttle", "detail": f"HTTP {meta['status']}"}
    if meta["head"] != "%PDF-":
        return {**res, "outcome": "not_pdf", "detail": f"HTTP {meta['status']} {meta['ct'][:40]}，不是 PDF"}
    if not meta["eof"]:
        return {**res, "outcome": "truncated", "detail": f"{meta['size']} 字节，缺 %%EOF"}
    part = Path(str(out) + ".part")
    step = 1 << 20
    with part.open("wb") as f:
        for a in range(0, meta["size"], step):
            f.write(base64.b64decode(page.evaluate(JS_CHUNK, [a, min(a + step, meta["size"])])))
    try:
        page.evaluate("() => { delete window.__litdl; }")
    except Exception:
        pass
    bad = pdf_problem(part)
    if bad:
        part.unlink(missing_ok=True)
        return {**res, "outcome": "truncated" if "截断" in bad or "0 页" in bad else "not_pdf", "detail": bad}
    os.replace(part, out)
    return {**res, "outcome": "ok", "detail": ""}


class DownloadWatch:
    """Collect download events from the recipe's own tab and from every tab opened after the watch started.

    Tabs that were already open are not listened to: a verification page left open for the user from an earlier
    paper can finish a download at any moment, and that file is not this paper's (review 2026-11-22)."""

    def __init__(self, ctx, own=None):
        self.ctx, self.items, self.pages = ctx, [], []
        self.initial = {id(p) for p in ctx.pages}         # tabs that already existed: not ours to inspect
        self._on_dl = lambda d: self.items.append(d)      # a plain function: Playwright cannot wrap a builtin method
        self._on_page = lambda p: self._hook(p)
        if own is not None:
            self._hook(own)
        ctx.on("page", self._on_page)

    def _hook(self, p):
        p.on("download", self._on_dl)
        self.pages.append(p)

    def close(self):
        try:
            self.ctx.remove_listener("page", self._on_page)
        except Exception:
            pass
        for p in self.pages:
            try:
                p.remove_listener("download", self._on_dl)
            except Exception:
                pass


JS_PDF_LINKS = r"""() => {
  const abs = h => { try { return new URL(h, location.href).href; } catch (e) { return ''; } };
  const out = [];
  const m = document.querySelector('meta[name="citation_pdf_url"]');
  if (m && m.content) out.push({how: 'meta', href: abs(m.content), text: 'citation_pdf_url', score: 9});
  let k = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    const t = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    let s = 0;
    if (/\.pdf(\?|#|$)/i.test(h)) s += 3;
    if (/\/pdf(ft|direct)?(\/|\?|$)|\/bitstreams?\/|\/download|Delivery\.cfm|ndownloader|viewcontent\.cgi|\/files?\//i.test(h)) s += 2;
    if (/\bpdf\b|download|下载|全文|full.?text/i.test(t)) s += 2;
    if (/suppl|supporting|appendix|_sm\d|si_\d|licen[cs]e|terms|policy|guide|help/i.test(h + ' ' + t)) s -= 5;
    if (/\/epdf\/|^javascript:|^#|^mailto:/i.test(h)) s -= 3;
    if (s >= 3) { a.setAttribute('data-litdl', String(k)); out.push({how: 'a', k: k++, href: abs(h), text: t, score: s}); }
  }
  return out.sort((x, y) => y.score - x.score).slice(0, 12);
}"""


def nav_fetch(env, page, url, out, timeout, hops=2, label="人机验证"):
    """Open `url` like a reader would. A PDF arrives as a browser download (the profile is set to download PDFs);
    a landing page is searched for its PDF link (citation_pdf_url, then scored <a> links) and that link is followed.
    A verification on the way is got past hands off (get_past) and the same step is looked at again afterwards.
    -> dict(outcome=ok|need_user|blocked|no_link|throttle|not_pdf|truncated|error, detail)"""
    watch = DownloadWatch(env.sess.ctx, page)
    try:
        status = None
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
        except Exception as e:
            msg = str(e)
            if "Download is starting" not in msg and "ERR_ABORTED" not in msg:
                return {"outcome": "error", "detail": msg.splitlines()[0][:140]}
        hop, gate_clicked = 0, False
        while True:
            t0 = time.time()
            while time.time() - t0 < (timeout if watch.items else 12):
                if watch.items:
                    return save_download(watch.items[-1], out, timeout)
                page.wait_for_timeout(700)
            # no download: what are we looking at? A click may have opened a new tab - but only a tab
            # opened by this call is ours; otherwise inspect the page we actually navigated.
            opened = [p for p in watch.pages
                      if id(p) not in watch.initial and not p.is_closed() and p.url not in ("about:blank", "")]
            view = opened[-1] if opened else page
            st = page_state(view)
            if st.get("ssrngate"):
                # SSRN's "sign up first" page: its own two buttons lead to the file (2026-11-22, #157: 1 MB came down).
                # Nothing is typed and the sign-up form's Cloudflare box is not touched
                if gate_clicked:
                    return {"outcome": "error", "detail": "SSRN 的注册提示页上点了“Download without registration”，文件没来"}
                gate_clicked = True
                say("       SSRN 的注册提示页：点它自己的“Download without registration”，再点“Download”")
                try:
                    view.locator(".show-download-module-btn").first.click(timeout=8000)
                    view.wait_for_timeout(800)
                    view.locator(".download-final-button").first.click(timeout=8000, no_wait_after=True)
                except Exception as e:
                    if "Download is starting" not in str(e):
                        return {"outcome": "error", "detail": f"SSRN 注册提示页上的下载按钮没点成：{str(e).splitlines()[0][:100]}"}
                status = None
                continue
            if st["challenge"]:
                watch.close()
                page, res = get_past(env, view, label, out)
                if res:
                    return res
                watch = DownloadWatch(env.sess.ctx, page)
                status = None
                continue                              # the same step again, on the page that came after the check
            if st.get("nodl"):
                # an answer about this copy, like a closed file: the ledger keeps it and the copy is not tried again
                return {"outcome": "no_access", "detail": f"页面写着 Not Available for Download，这份副本不提供全文：{st['title'][:40]}"}
            if hop and st.get("denied"):
                # the landing page opened and only the file is closed (campus-only, embargo): an answer about this copy,
                # not a block on the site
                return {"outcome": "no_access", "detail": f"落地页能开，文件限制访问（多为限校内或禁发期）：{st['title'][:40]}"}
            if st["blocked"]:
                return {"outcome": "blocked", "detail": f"页面提示被限制访问：{st['title'][:40]}"}
            if status in THROTTLE:
                return {"outcome": "throttle", "detail": f"HTTP {status}"}
            if hop == hops:
                break
            links = view.evaluate(JS_PDF_LINKS)
            if not links:
                return {"outcome": "no_link", "detail": f"页面上没找到 PDF 链接：{st['title'][:50]}"}
            best = links[0]
            say(f"       页面里的 PDF 链接：{best['text'][:30]!r} {best['href'][:90]}")
            try:
                if best["how"] == "a":
                    view.locator(f'[data-litdl="{best["k"]}"]').first.click(timeout=8000, no_wait_after=True)
                else:
                    view.evaluate("h => { location.assign(h); }", best["href"])
            except Exception:
                try:
                    view.evaluate("h => { location.assign(h); }", best["href"])
                except Exception as e:
                    if "Download is starting" not in str(e):
                        return {"outcome": "error", "detail": str(e).splitlines()[0][:140]}
            status = None
            hop += 1
        return {"outcome": "no_link", "detail": "跟着链接走了两步还是没有 PDF"}
    finally:
        watch.close()


def save_download(dl, out, timeout):
    part = Path(str(out) + ".part")
    try:
        dl.save_as(str(part))                 # waits for the browser to finish the download
    except Exception as e:
        return {"outcome": "error", "detail": f"下载没完成：{str(e).splitlines()[0][:100]}"}
    fail = dl.failure()
    if fail:
        part.unlink(missing_ok=True)
        return {"outcome": "error", "detail": f"下载失败：{fail}"}
    bad = pdf_problem(part)
    size = part.stat().st_size
    if bad:
        name = dl.suggested_filename
        part.unlink(missing_ok=True)
        return {"outcome": "truncated" if "截断" in bad or "0 页" in bad else "not_pdf", "size": size, "detail": f"{bad}（{name}）"}
    os.replace(part, out)
    return {"outcome": "ok", "size": size, "detail": dl.suggested_filename}


# --------------------------------------------------------------------------- hands off: a verification judged with nothing attached
# 2026-11-22, same Chrome, same exit IP, same paper. With Playwright attached, ScienceDirect's PDF verification
# never let a click through (11-19, 11-21, and 09:19 that morning: box clicked, page stayed on "请稍候…"). With
# nothing attached while the page loaded, one click on each of its two checkboxes and the PDF was there 3 s later.
# The exit address was the same overseas one both times; what differed was the automation attached to the tab.
# The same day the user made it the rule for every verification ("如果遇到验证，都用这个方式过，就是松开脚本再过"):
# when a recipe meets one (get_past), it lets go of the browser, opens the same address again through the
# debugging port's plain HTTP endpoint (which attaches nothing) and gets past it there: a known checkbox or
# Sci-Hub's own button with the system mouse (Windows only), a click / slider puzzle with the vision model when
# one is configured, the user otherwise. While the page is up the script watches only the tab list and the
# download folder, and reads the element tree (and, for the model, a screenshot) for a moment at a time over a
# bare DevTools connection that never turns on the Runtime domain. Once the page has moved on, the session
# attaches again and the recipe goes on in that same tab. With the captcha switch off it clicks nothing: the page
# waits in front for the user, whose click now counts as well.
DL_DIR = HOME / "downloads"
CHALLENGE_TITLE = re.compile(r"请稍候|just a moment|are you a robot|security verification|request verification|"
                             r"attention required|verify you are human|确认您是真人|正在检查|one more step|"
                             r"是机器人吗|вы робот", re.I)
# the same words JS_STATE looks for, for pages whose title says nothing (read from the element tree)
CHALLENGE_WORDS = re.compile(r"just a moment|请稍候|verify you are human|确认您是真人|are you a robot|是机器人吗|"
                             r"checking your browser|needs to review the security|人机验证|安全验证|滑动验证", re.I)
CHALLENGE_DONE = re.compile(r"验证成功|验证通过|verification (was )?successful", re.I)
# pages that carry a verification widget but are not a verification: SSRN's "sign up first" page, whose Cloudflare box
# belongs to the sign-up form (2026-11-22, #157; the way on is its "Download without registration" button)
NOT_A_CHALLENGE = re.compile(r"download without registration", re.I)
# ScienceDirect shows two verifications in a row (Cloudflare's, then its own "Request Verification"). Each gets at
# most two clicks, the page at most four. A click is followed by a wait for the page to move on: Turnstile spins
# for a few seconds after a click, and clicking again into the spinner only spoils it. The first click waits until
# the widget has been up for a while: clicked while it was still loading, it did nothing (2026-11-22, row 2).
HANDSOFF_CLICKS = 4
HANDSOFF_PER_STEP = 2
HANDSOFF_SETTLE = 5                   # seconds a verification page is up before the first click on it
HANDSOFF_AFTER = 12                   # seconds after a click before the same page is clicked again
HANDSOFF_LIMIT = 90                   # seconds a reopened page gets to clear before it counts as stuck (the user's
                                      # wait, --human-wait, comes on top once they are asked)
HANDSOFF_PASSES = 3                   # verifications one paper may meet; one that keeps coming back goes to the user
# controls on a verification page that are buttons rather than checkbox iframes: Sci-Hub's "不是" (ALTCHA)
VERIFY_BUTTONS = (("onclick", "check()"),)


def devtools(path, method="GET"):
    """The debugging port's HTTP endpoint (/json/list, /json/new?<url> with PUT, /json/close/<id>). Attaches nothing."""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


def tab_info(tid):
    try:
        return next((t for t in devtools("/json/list") or [] if t.get("id") == tid), None)
    except Exception:
        return None


def bare_tabs():
    """The browser's tabs from the debugging port's list: attaches nothing."""
    try:
        return [t for t in devtools("/json/list") or [] if t.get("type") == "page"]
    except Exception:
        sys.exit("专用 Chrome 没在运行（调试端口连不上），先跑 litbrowser open")


def tab_ids():
    try:
        return {t["id"] for t in devtools("/json/list") or [] if t.get("type") == "page"}
    except Exception:
        return set()


def close_tab(tid):
    try:
        devtools(f"/json/close/{tid}")
    except Exception:
        pass


def bare_find(key):
    """A tab by id prefix, or by a piece of its address or title, from the bare list (see Session.find)."""
    tabs = bare_tabs()
    hits = [t for t in tabs if t["id"].lower().startswith(key.lower())]
    if not hits:
        hits = [t for t in tabs if key.lower() in (t.get("url") or "").lower() or key.lower() in (t.get("title") or "").lower()]
    if len(hits) != 1:
        sys.exit(f"--tab {key!r} 匹配到 {len(hits)} 个标签页；先跑 tabs 看清楚")
    return hits[0]


class BareTab:
    """A short DevTools connection to one tab: plain websocket, no Playwright, Runtime never enabled."""

    def __init__(self, ws_url):
        self.ws_url, self.ws, self.n = ws_url, None, 0

    def __enter__(self):
        from websockets.sync.client import connect
        self.ws = connect(self.ws_url, proxy=None, max_size=2 ** 25, open_timeout=5, close_timeout=2)
        return self

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=10))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}

    def __exit__(self, *exc):
        try:
            self.ws.close()
        except Exception:
            pass


def bare_browser_pid():
    """PID of the dedicated Chrome's browser process, over the browser-level DevTools endpoint (no tab is attached)."""
    try:
        with BareTab(devtools("/json/version")["webSocketDebuggerUrl"]) as b:
            for p in b.send("SystemInfo.getProcessInfo").get("processInfo", []):
                if p.get("type") == "browser":
                    return int(p["id"])
    except Exception:
        pass
    return None


def verify_buttons(root):
    """[(kind, nodeId)] of verification buttons (VERIFY_BUTTONS) in a DOM.getDocument(pierce=True) tree."""
    found = []

    def walk(node, depth=0):
        if depth > 40:
            return
        attrs = node.get("attributes") or []
        attrs = {attrs[i]: attrs[i + 1] for i in range(0, len(attrs) - 1, 2)}
        if any(attrs.get(k, "").replace(" ", "") == v for k, v in VERIFY_BUTTONS):
            found.append(("button", node.get("nodeId")))
        for key in ("children", "shadowRoots", "contentDocument", "pseudoElements", "templateContent"):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child, depth + 1)
            elif isinstance(child, list):
                for item in child:
                    walk(item, depth + 1)

    walk(root)
    return found


def bare_viewport(t):
    vv = t.send("Page.getLayoutMetrics").get("cssVisualViewport") or {}
    return vv.get("clientWidth") or 0, vv.get("clientHeight") or 0


def bare_look(ws_url):
    """(controls, view_w, view_h, text) of a tab: checkbox iframes and verification buttons with CSS boxes, viewport
    size, start of its text."""
    with BareTab(ws_url) as t:
        root = t.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
        hits = []
        for kind, node_id in captcha_ai.widget_nodes(root) + verify_buttons(root):
            try:
                q = t.send("DOM.getBoxModel", {"nodeId": node_id})["model"]["content"]
            except Exception:                         # not rendered (display: none): not a control to click
                continue
            xs, ys = q[0::2], q[1::2]
            box = {"kind": kind, "x": min(xs), "y": min(ys), "w": max(xs) - min(xs), "h": max(ys) - min(ys)}
            if box["w"] >= 24 and box["h"] >= 18:
                hits.append(box)
        vw, vh = bare_viewport(t)
        try:
            html = t.send("DOM.getOuterHTML", {"nodeId": root["nodeId"]}).get("outerHTML", "")
        except Exception:
            html = ""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html[:400000])))
    # not cut to a few thousand characters: bare_challenge tells a verification page from a long page that only
    # carries a widget by this length
    return hits, vw, vh, text[:20000]


def bare_shot(ws_url, fmt="jpeg"):
    """(image bytes, view_w, view_h): a screenshot of the tab's viewport over a bare connection."""
    with BareTab(ws_url) as t:
        vw, vh = bare_viewport(t)
        r = t.send("Page.captureScreenshot", {"format": fmt, **({"quality": 80} if fmt == "jpeg" else {})})
    return base64.b64decode(r["data"]), vw, vh


def bare_challenge(title, look=None):
    """Is a tab showing a verification? Its title first; then, when read, its element tree (bare_look)."""
    if CHALLENGE_TITLE.search(title or ""):
        return True
    if look is None:
        return False
    hits, _, _, text = look
    head = (title or "") + " " + text[:800]
    if CHALLENGE_DONE.search(head) or NOT_A_CHALLENGE.search(text[:5000]):
        return False
    return bool(hits and len(text) < 5000) or bool(CHALLENGE_WORDS.search(head[:700]) and len(text) < 1500)


def hit_point(h):
    """Where to click a control found by bare_look: a button in its middle, a checkbox widget on its square."""
    if h["kind"] == "button":
        return h["x"] + h["w"] / 2, h["y"] + h["h"] / 2
    return captcha_ai._checkbox_point(h["kind"], h)


def handsoff_act(hwnd, vw, vh, plan):
    """Carry out a vision-model plan (CSS pixels) with the system mouse. A "checkbox" the model sees without a known
    widget behind it is never clicked (see captcha_ai.solve)."""
    import osmouse
    kind = plan.get("type")
    if kind == "slider":
        hx, hy = captcha_ai._center(plan["handle"])
        gap = captcha_ai._center(plan["gap"])
        piece = captcha_ai._center(plan["piece"]) if plan.get("piece") else None
        dx = gap[0] - (piece[0] if piece else hx)
        dy = 0.0 if abs(gap[1] - hy) > 40 else gap[1] - hy
        return osmouse.drag_css(hwnd, vw, vh, hx, hy, hx + dx, hy + dy, duration=random.uniform(0.65, 0.95))
    if kind == "click":
        done = False
        for p in plan.get("points") or []:
            if not osmouse.click_css(hwnd, vw, vh, p[0], p[1]):
                return done
            done = True
            time.sleep(random.uniform(0.3, 0.7))
        if plan.get("submit"):
            time.sleep(0.4)
            done = osmouse.click_css(hwnd, vw, vh, plan["submit"][0], plan["submit"][1]) or done
        return done
    return False


def handsoff_model(tid, ws_url, pid, title, url, look, cfg):
    """One vision-model attempt on a tab with nothing attached: bare screenshot -> plan -> system mouse."""
    import osmouse
    try:
        devtools(f"/json/activate/{tid}")
        time.sleep(0.4)
        raw, cw, ch = bare_shot(ws_url)
    except Exception as e:
        say(f"       截不到验证页：{str(e)[:80]}")
        return False
    plan, err = captcha_ai.plan_shot(captcha_ai.shot_from_bytes(raw, cw, ch), title, url, look[3] if look else "", cfg)
    if not plan or plan.get("type") not in ("click", "slider"):
        why = err or (plan or {}).get("note") or (plan or {}).get("type") or ""
        say(f"       模型没给出能做的操作{('（' + str(why)[:60] + '）') if why else ''}")
        return False
    hwnd = osmouse.window_for(pid, title)
    ok = bool(hwnd) and handsoff_act(hwnd, cw, ch, plan)
    say(f"       模型：{'拖滑块' if plan['type'] == 'slider' else '依次点 ' + str(len(plan.get('points') or [])) + ' 处'}，"
        f"{'系统鼠标已做' if ok else '专用 Chrome 的窗口切不到前台，没做'}")
    return ok


PARTIAL = (".crdownload", ".tmp", ".part")


def finished_downloads(before, want=""):
    """-> (finished files that are this paper's, newest last; is a new download still being written?)

    With `want` (a piece of the file name only this paper's file carries, e.g. ScienceDirect's PII) only those files
    count, whenever they arrived: a file another tab finishes meanwhile (a verification page left open for the
    user, a slow download of the paper before) is not taken for this one (review 2026-11-22). Without it, any
    finished file that is not in `before` counts, and nothing does while a new partial file is being written."""
    try:
        names = {p.name for p in DL_DIR.iterdir()}
    except FileNotFoundError:
        return [], False
    writing = any(n.endswith(PARTIAL) for n in names - before)
    if want:
        hit = [n for n in names if want.lower() in n.lower() and not n.endswith(PARTIAL)]
    else:
        hit = [] if writing else [n for n in names - before if not n.endswith(PARTIAL) and not n.startswith(".")]
    return sorted((DL_DIR / n for n in hit), key=lambda p: p.stat().st_mtime), writing


def take_download(path, out):
    """Move a finished file from the download folder to the stage, if it is a whole PDF."""
    size0 = path.stat().st_size
    time.sleep(1.0)
    if path.stat().st_size != size0:              # still growing
        return None
    bad = pdf_problem(path)
    size = path.stat().st_size
    if bad:
        target = Path(str(out) + ".rejected")
        move_file(path, target)
        return {"outcome": "truncated" if "截断" in bad or "0 页" in bad else "not_pdf", "size": size,
                "detail": f"{bad}（{path.name}，留在 {target.name}）"}
    move_file(path, out)
    return {"outcome": "ok", "size": size, "detail": path.name}


def move_file(src, dst, tries=5):
    """shutil.move (the stage may be on another drive than ~/.litdl) with a few retries while something such as a
    virus scanner still holds the file that was just written."""
    import shutil
    for k in range(tries):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(1.5)


def handsoff_ok():
    if os.environ.get("LITDL_HANDSOFF", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    try:
        import websockets.sync.client  # noqa: F401
    except ImportError:
        return False
    return True


def handsoff_fetch(env, url, out, pid, label, limit, judge=None, want="", until_clear=False, replace=""):
    """Open `url` in a new tab with the session released, get past the verification it shows, wait for what comes.

    Call it only while the session is released (see get_past, r_elsevier).
    - A finished download in the download folder (only one whose name carries `want`, when given) is this paper's
      file and is moved to `out`.
    - until_clear: the page moving past the verification is a success too -> {"outcome": "passed", "tab": <target
      id>, ...}, and that tab is left open for the caller to attach to. Without it (ScienceDirect's PDF page) only
      the file counts, and `judge(text)` classifies a page that is neither a verification nor a download.
    - replace: target id of a tab to close once the new one is open (the attached tab the verification first
      showed in; its load was spoilt by the automation).
    Every other ending closes the tab."""
    import osmouse
    cfg = captcha_ai.config(HOME)
    auto = cfg["enabled"] and osmouse.available() and captcha_ai._os_mouse_enabled()
    DL_DIR.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in DL_DIR.iterdir()}
    tab = devtools("/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
    tid = tab["id"]
    if replace and replace != tid:
        close_tab(replace)
    t0 = time.time()
    clicks, last_click, first_seen, last_look, asked, deadline = 0, 0.0, None, 0.0, False, t0 + limit
    url_now, quiet_since, step_clicks, step_acts, ch_since, clear_since = "", t0, 0, 0, None, None
    no_window, model_left = False, (cfg["attempts"] if auto and cfg["ready"] else 0)
    hard_end = t0 + limit + 600                   # a download still being written may run on, but not for ever
    keep = False

    def take(got):
        res = take_download(got[-1], out)
        if res and clicks:
            res["detail"] = f"点了 {clicks} 次验证后下到；{res['detail']}"
        return res

    try:
        while time.time() < deadline:
            got, writing = finished_downloads(before, want)
            if got:
                result = take(got)
                if result:
                    return result
            if writing and deadline - time.time() < 20:     # a large file is still coming in: not a failed verification
                deadline = min(hard_end, deadline + 30)
            info = tab_info(tid)
            now = time.time()
            if info is None:                          # the tab went away: a download that opened in it closes it
                time.sleep(1.5)
                continue
            title, cur, ws = info.get("title") or "", info.get("url") or "", info.get("webSocketDebuggerUrl")
            if cur != url_now:                        # a new page: the next verification step, or what came after one
                url_now, quiet_since, step_clicks, step_acts, ch_since, clear_since = cur, now, 0, 0, None, None
            if "sign_in" in cur:
                return {"outcome": "login", "detail": "WebVPN 会话失效"}
            look = None
            challenge = bool(CHALLENGE_TITLE.search(title))
            if until_clear and not challenge and ws and now - last_look > 2.5:
                last_look = now                       # the title says nothing: ask the element tree
                try:
                    look = bare_look(ws)
                except Exception:
                    look = None
                challenge = bare_challenge(title, look)
                if not challenge and look is not None and not writing:
                    loaded = bool(title) and title != cur and not title.startswith(("http:", "https:", "about:"))
                    if loaded or now - quiet_since > 15:
                        clear_since = clear_since or now
                        if now - clear_since >= 4:    # two clear looks in a row, 4 s apart: it has moved on
                            keep = True
                            return {"outcome": "passed", "tab": tid, "url": cur, "clicks": clicks,
                                    "detail": f"{label}：{'点了 ' + str(clicks + step_acts) + ' 次后' if clicks or step_acts else '没点就'}过了"}
            if challenge:
                clear_since = None
                first_seen = first_seen or now
                ch_since = ch_since or now
                if (auto and not asked and not no_window and now - ch_since > HANDSOFF_SETTLE
                        and now - last_click > HANDSOFF_AFTER and ws and (look is not None or now - last_look > 2.5)):
                    if look is None:
                        last_look = now
                        try:
                            look = bare_look(ws)
                        except Exception as e:
                            say(f"       读不到验证页的元素：{str(e)[:80]}")
                    hits, vw, vh = (look[0], look[1], look[2]) if look else ([], 0, 0)
                    if hits and vw and vh and clicks < HANDSOFF_CLICKS and step_clicks < HANDSOFF_PER_STEP:
                        h = hits[0]
                        x, y = hit_point(h)
                        try:
                            devtools(f"/json/activate/{tid}")    # our tab in front of its window; attaches nothing
                            time.sleep(0.4)
                        except Exception:
                            pass
                        hwnd = osmouse.window_for(pid, title)
                        if hwnd and osmouse.click_css(hwnd, vw, vh, x, y):
                            clicks += 1
                            step_clicks += 1
                            last_click = time.time()
                            say(f"       浏览器已松开；识别到 {h['kind']}，系统鼠标点 ({x:.0f},{y:.0f})，"
                                f"这一页第 {step_clicks} 次，共 {clicks} 次")
                        else:
                            seen = [osmouse._window_text(w) for w in osmouse._chrome_windows()
                                    if osmouse._window_pid(w) == pid] if osmouse.available() and pid else []
                            say(f"       {'专用 Chrome 的窗口切不到前台' if hwnd else '找不到显示这一页的专用 Chrome 窗口'}，不点，交给用户（找的是 {title[:30]!r}，"
                                f"专用 Chrome 的窗口：{[s_[:40] for s_ in seen]}，前台是 {osmouse.foreground_text()[:40]!r}）")
                            no_window = True
                    elif not hits and model_left and vw and vh and now - ch_since > 8:
                        model_left -= 1               # no known control: a click / slider puzzle for the vision model
                        step_acts += 1
                        if not handsoff_model(tid, ws, pid, title, cur, look, cfg):
                            model_left = 0
                        last_click = time.time()
                spent = (no_window or clicks >= HANDSOFF_CLICKS or step_clicks >= HANDSOFF_PER_STEP
                         or (step_acts and not model_left))
                nothing = step_clicks == 0 and step_acts == 0 and now - ch_since > 40   # nothing found to click on it
                if not asked and (not auto or (spent and now - last_click > HANDSOFF_AFTER) or nothing):
                    asked = True
                    try:
                        devtools(f"/json/activate/{tid}")         # the page the user is asked to click, in front
                        time.sleep(0.4)
                    except Exception:
                        pass
                    hwnd = osmouse.window_for(pid, title)
                    if hwnd:
                        osmouse.focus(hwnd)
                    say(f"NEED_USER  {label}，请您在专用 Chrome 前台那一页点一下（脚本已松开浏览器，您点的才算数）  "
                        f"标签页 {tid[:6]}：{title!r} {cur[:90]}  （等 {env.human_wait} 秒）")
                    if env.prog:
                        env.prog.need_user(label, cur, tid[:6])
                    deadline = max(deadline, time.time() + env.human_wait)
            elif judge and now - quiet_since > 12 and now - last_look > 6 and ws:
                last_look = now                   # neither a verification nor a file after a while: read what it says
                try:
                    verdict = judge(bare_look(ws)[3])
                except Exception:
                    verdict = None
                if verdict:
                    return verdict
            time.sleep(1.5)
        got, writing = finished_downloads(before, want)      # the file may have landed during the last wait
        if got:
            result = take(got)
            if result:
                return result
        if writing:
            return {"outcome": "error", "detail": f"{label}：文件还在下（{round(time.time() - t0)} 秒），没等到下完；"
                    f"下完会留在 {DL_DIR}，下一轮会认出它"}
        if first_seen:
            return {"outcome": "need_user", "detail": f"{label}：验证没通过（浏览器已松开，点了 {clicks + step_acts} 次"
                    f"{'，也等了用户' if asked else ''}），已关掉该页"}
        if until_clear:
            return {"outcome": "error", "detail": f"{label}：重新打开的页面 {round(time.time() - t0)} 秒没打开完"}
        return {"outcome": "error", "detail": f"打开 PDF 链接 {round(time.time() - t0)} 秒没有下载开始"}
    finally:
        if asked and env.prog:
            env.prog.need_user(None, None)
        if not keep:
            close_tab(tid)


def reopen_url(url):
    """The address to open again: without the tokens Cloudflare's check writes into the address bar while it runs
    (__cf_chl_rt_tk=… and the like), which belong to the spoilt attempt."""
    parts = urllib.parse.urlsplit(url)
    if "__cf_chl" not in parts.query:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("__cf_chl")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def get_past(env, page, label, out):
    """A verification is showing in `page`. Let go of the browser, open the same address again with nothing
    attached, get past it there (handsoff_fetch), attach again.

    -> (page, result). result None: the page moved past the verification, and `page` is the attached tab showing
    what came after it; the recipe goes on there. Otherwise `result` is this paper's answer (the file arrived while
    the browser was let go: ok; nobody got past it: need_user; the login is gone: login) and `page` is None.
    Page objects the caller held before are dead afterwards (Session.release). Without the hands-off path
    (LITDL_HANDSOFF=0, or no websockets package) this is the older wait on the attached page (wait_human)."""
    env.passes += 1
    if env.passes > HANDSOFF_PASSES:
        return page, {"outcome": "need_user", "detail": f"{label}：这一篇已经过了 {HANDSOFF_PASSES} 次验证，又出现，不再重开，交给用户"}
    if not handsoff_ok():
        if wait_human(page, label + "，请您点一下", env.human_wait, env.prog):
            return page, None
        return page, {"outcome": "need_user", "detail": f"{label}：没人点"}
    import osmouse
    url, old, pid = reopen_url(page.url), env.sess.target_id(page), osmouse._browser_pid(page)
    try:
        page.goto("about:blank")              # this load of the page was made with automation attached: spoilt
    except Exception:
        pass
    say(f"       {label}：松开浏览器，重新打开这一页再过验证")
    env.sess.release()
    try:
        res = handsoff_fetch(env, url, out, pid, label, HANDSOFF_LIMIT, until_clear=True, replace=old)
    except Exception as e:
        res = {"outcome": "error", "detail": f"{label}：松开浏览器重开这一页时出错：{str(e).splitlines()[0][:100]}"}
    try:
        env.sess.reattach()
    except Exception as e:
        say(f"       接回浏览器失败：{str(e).splitlines()[0][:100]}")     # cmd_fetch tries once more and stops
        if res.get("outcome") == "passed":
            res = {"outcome": "error", "detail": f"{label}：过了验证，但接不回浏览器"}
        return None, res
    if res.get("outcome") != "passed":
        return None, res
    p = env.sess.page_by_id(res["tab"])
    if p is None:
        close_tab(res["tab"])
        return None, {"outcome": "error", "detail": f"{label}：过了验证，但接回浏览器后找不到那个标签页"}
    say(f"       {res['detail']}，接着做")
    return p, None


def await_file(env, page, watch, out, recipe, label, seconds=None):
    """After a click on a PDF link: wait for the browser's download. A verification that comes up meanwhile, in this
    tab or in one the click opened, is got past hands off (get_past); the file then usually arrives while the browser
    is let go. -> (result, page): result None when nothing came within `seconds`; page is the tab to go on with."""
    limit = seconds or env.timeout(recipe)
    t0, look, own = time.time(), time.time(), None
    try:
        while time.time() - t0 < limit:
            if watch.items:
                return save_download(watch.items[-1], out, env.timeout(recipe)), page
            page.wait_for_timeout(1000)
            if time.time() - look < 4:
                continue
            look = time.time()
            views = [p for p in watch.pages if id(p) not in watch.initial and not p.is_closed() and p is not page]
            for v in views + [page]:
                if not page_state(v)["challenge"]:
                    continue
                watch.close()
                page, res = get_past(env, v, label, out)
                if res:
                    return res, None
                own = watch = DownloadWatch(env.sess.ctx, page)
                t0 = time.time()
                break
        return None, page
    finally:
        if own:
            own.close()


# --------------------------------------------------------------------------- recipes (one paper each)
def full_title(row):
    return row["title"] + ((" " + row["subtitle"]) if row.get("subtitle") else "")


JS_EBSCO = r"""() => {
  const rec = (a, key) => ((a.getAttribute('href') || '').split(key)[1] || '').split(/[?#\/]/)[0];
  const det = [...document.querySelectorAll('a[href*="/search/details/"]')]
      .map(a => ({rec: rec(a, '/search/details/'), t: (a.innerText || '').trim()})).filter(x => x.rec && x.t);
  const pdf = [...document.querySelectorAll('a[href*="/viewer/pdf/"]')].map(a => rec(a, '/viewer/pdf/'));
  const body = document.body ? document.body.innerText : '';
  return {det, pdf, none: /没有找到|未找到任何|no results (were )?found|找不到结果/i.test(body), path: location.pathname, host: location.host};
}"""


def r_ebsco(env, page, row, out):
    """INFORMS and other business journals through EBSCO on the WebVPN: search by title, match the record, call the PDF API."""
    base = f"{litdl.vpn_host('research.ebsco.com', env.vpn)}/c/{env.opid}"
    want = litdl.norm(full_title(row))
    q = re.sub(r"[^\w\s\-&',]", " ", full_title(row))
    page.goto(f"{base}/search/results?q={urllib.parse.quote(q)}&autocorrect=y", wait_until="domcontentloaded", timeout=60000)
    hit, t0, st = None, time.time(), {}
    while time.time() - t0 < 75:                 # the page redirects once to add database params; results take 20-25 s
        page.wait_for_timeout(2500)
        try:
            st = page.evaluate(JS_EBSCO)
        except Exception:
            continue
        if "sign_in" in st.get("path", "") or env.vpn not in st.get("host", env.vpn):
            return {"outcome": "login", "detail": "WebVPN 会话失效，跳到了登录页"}
        ps = page_state(page)
        if ps["challenge"]:
            page, res = get_past(env, page, "EBSCO 人机验证", out)
            if res:
                return res
            t0 = time.time()                     # the time spent on the verification is not the search's
            continue
        for d in st["det"]:                      # never take the first result blindly: unrelated papers get mixed in
            d["t"] = litdl.norm(d["t"])          # same normaliser on both sides (dashes, accents, "&")
        hit = litdl.pick_title(want, st["det"])
        if hit or st["none"]:
            break
    if not hit:
        seen = "; ".join(d["t"][:40] for d in st.get("det", [])[:3])
        return {"outcome": "not_found", "detail": f"EBSCO 搜不到（看到的：{seen or '无结果'}）"}
    t1 = time.time()
    has_pdf = hit["rec"] in st["pdf"]
    while not has_pdf and time.time() - t1 < 8:  # the PDF link renders a few seconds after the title
        page.wait_for_timeout(2000)
        try:
            has_pdf = hit["rec"] in page.evaluate(JS_EBSCO)["pdf"]
        except Exception:
            pass
    if not has_pdf:
        return {"outcome": "citation_only", "detail": "EBSCO 有题录无全文"}
    api = (f"/api/search/v1/record/{hit['rec']}/fulltext/pdf?sourceRecordId={hit['rec']}"
           f"&opid={env.opid}&intent=download&lang=zh-CN")
    return inpage_fetch(page, api, out, env.timeout("ebsco"))


def vpn_links(env, row):
    """(page, pdf) addresses through the WebVPN, built now from the school's settings. The manifest keeps the ones
    made when it was planned, and those go stale when `portal --save` learns afterwards that this school writes a
    host differently (2026-11-21: Springer's :443 form, learnt after plan; every request then failed)."""
    page, pdf = litdl.webvpn_links(row, env.vpn) if env.vpn else ("", "")
    return page or row.get("webvpn_page") or "", pdf or row.get("webvpn_pdf") or ""


def vpn_page(env, row):
    return vpn_links(env, row)[0]


def r_wiley(env, page, row, out):
    """Wiley through the WebVPN: subscribed journals show 'Full Access'; the PDF is /doi/pdfdirect/<doi>."""
    page.goto(vpn_page(env, row), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    ps = page_state(page)
    if ps["signin"] or "sign_in" in page.url:
        return {"outcome": "login", "detail": "WebVPN 会话失效"}
    if ps["challenge"]:
        page, res = get_past(env, page, "Wiley 人机验证", out)
        if res:
            return res
    if "/doi/abs/" in page.url:
        return {"outcome": "no_access", "detail": "Wiley 跳到 /doi/abs，学校没订这一刊（期）"}
    res = inpage_fetch(page, f"/doi/pdfdirect/{row['doi']}?download=true", out, env.timeout("wiley"))
    if res["outcome"] == "throttle" and res.get("status") == 403:
        body = (page.inner_text("body") or "")[:4000]
        if "Full Access" not in body and "Open Access" not in body and "Free Access" not in body:
            return {**res, "outcome": "no_access", "detail": "Wiley 返回 403，页面没有 Full Access 标记，学校没订"}
    return res


def r_direct(env, page, row, out):
    """Springer / IEEE through the WebVPN: open the article page, then fetch the known PDF address on the same origin."""
    page_url, pdf_url = vpn_links(env, row)
    page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    if "sign_in" in page.url:
        return {"outcome": "login", "detail": "WebVPN 会话失效"}
    ps = page_state(page)
    if ps["challenge"]:
        page, res = get_past(env, page, "出版商人机验证", out)
        if res:
            return res
    pdf = urllib.parse.urlsplit(pdf_url)
    here = urllib.parse.urlsplit(page.url)
    # Springer's portal host redirects (…-com.<vpn>:443 -> …-com-s.<vpn>); keep the path, use the page's own origin
    url = pdf.path + (("?" + pdf.query) if pdf.query else "") if here.netloc.split(":")[0].split(".")[1:] == pdf.netloc.split(":")[0].split(".")[1:] else pdf_url
    res = inpage_fetch(page, url, out, env.timeout("direct"))
    if res["outcome"] == "not_pdf":
        # IEEE, 2026-11-21: the first request for the PDF right after the article page opened came back as a 404
        # page; the same request a little later gave the file. One more try after a pause, then the answer stands.
        page.wait_for_timeout(7000)
        res = inpage_fetch(page, url, out, env.timeout("direct"))
    return res


JS_SD = r"""(pii) => {
  const a = [...document.querySelectorAll('a[href]')].find(x => (x.getAttribute('href') || '').includes('/pii/' + pii + '/pdfft'));
  const body = document.body ? document.body.innerText : '';
  const acc = (body.match(/Full text access|Open access|Get Access|Purchase PDF|There was a problem providing the content/i) || [''])[0];
  if (a) a.setAttribute('data-litdl', 'sd');
  return {link: !!a, acc};
}"""


def sd_judge(text):
    """What a ScienceDirect PDF tab says when it is neither a verification nor a download."""
    if re.search(r"problem providing the content", text, re.I):
        ip = (re.search(r"IP(?: Address)?:\s*([\d.]+)", text) or [None, ""])[1]
        return {"outcome": "blocked", "detail": "ScienceDirect：There was a problem providing the content"
                + (f"（页面看到的出口 IP 是 {ip}）" if ip else "")}
    if re.search(r"get access|purchase pdf", text, re.I):
        return {"outcome": "no_access", "detail": "ScienceDirect 只有 Get Access / Purchase PDF，学校没订"}
    return None


def r_elsevier(env, page, row, out):
    """ScienceDirect through the WebVPN. Only the PDF link printed on the page is followed; the address is never built by hand."""
    page.goto(vpn_page(env, row), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    if "sign_in" in page.url:
        return {"outcome": "login", "detail": "WebVPN 会话失效"}
    ps = page_state(page)
    if ps["challenge"]:
        page, res = get_past(env, page, "ScienceDirect 人机验证", out)
        if res:
            return res
        ps = page_state(page)
    info = {"link": False, "acc": ""}
    for _ in range(10):                          # the "View PDF" link shows up later than the access label ("Finding PDF…")
        try:
            info = page.evaluate(JS_SD, row["pii"])
        except Exception:
            pass
        if info["link"] or re.search(r"get access|purchase|problem providing", info["acc"], re.I):
            break
        page.wait_for_timeout(3000)
    if "problem providing" in info["acc"].lower() or ps["blocked"]:
        return {"outcome": "blocked", "detail": "ScienceDirect：There was a problem providing the content（出口 IP 被限）"}
    if not info["link"]:
        if re.search(r"get access|purchase", info["acc"], re.I):
            return {"outcome": "no_access", "detail": "ScienceDirect 只有 Get Access / Purchase PDF，学校没订"}
        return {"outcome": "no_link", "detail": "ScienceDirect 页面没渲染出 PDF 链接（不自己拼地址），过一阵再试或交给用户"}
    if handsoff_ok():
        # the PDF page sits behind a Cloudflare verification that does not pass while Playwright is attached
        # (see "hands off" above): take the printed link, let go of the browser, open it with nothing attached
        href = page.evaluate("() => { const a = document.querySelector('[data-litdl=\"sd\"]'); return a ? a.href : ''; }")
        if href:
            import osmouse
            pid = osmouse._browser_pid(page)
            try:
                page.goto("about:blank")              # the article tab is done; tidy() closes it later
            except Exception:
                pass
            env.sess.release()
            try:
                # ScienceDirect names the file 1-s2.0-<PII>-main.pdf: only that file is this paper's
                return handsoff_fetch(env, href, out, pid, "ScienceDirect 的 PDF 页在等人机验证",
                                      env.timeout("elsevier"), judge=sd_judge, want=row.get("pii") or "")
            finally:
                try:
                    env.sess.reattach()
                except Exception as e:                # keep the result (the file may already be in the stage);
                    say(f"       接回浏览器失败：{str(e).splitlines()[0][:100]}")   # cmd_fetch tries once more and stops
    watch = DownloadWatch(env.sess.ctx, page)
    try:
        page.locator('[data-litdl="sd"]').first.click(timeout=8000, no_wait_after=True)
        t0, rounds = time.time(), 0
        while time.time() - t0 < env.timeout("elsevier"):
            if watch.items:
                return save_download(watch.items[-1], out, env.timeout("elsevier"))
            page.wait_for_timeout(1000)
            for v in [p for p in watch.pages
                      if id(p) not in watch.initial and not p.is_closed() and p is not page]:
                st = page_state(v)
                if st["blocked"]:
                    return {"outcome": "blocked", "detail": "ScienceDirect：There was a problem providing the content"}
                if st["challenge"]:
                    rounds += 1
                    passed = rounds <= 2 and wait_human(v, "ScienceDirect 的 PDF 页在等人机验证，请您点一下", env.human_wait, env.prog)
                    if not passed:
                        # the page reloads itself while it waits: do not leave it hammering the server
                        ip = ""
                        try:
                            ip = (re.search(r"IP Address:\s*([\d.]+)", v.inner_text("body")) or [None, ""])[1]
                            v.close()
                        except Exception:
                            pass
                        return {"outcome": "need_user", "detail": "ScienceDirect 的 PDF 页人机验证没通过（验证框不出现或反复刷新），已关掉该页"
                                + (f"；页面看到的出口 IP 是 {ip}" if ip else "")}
                    t0 = time.time()
        return {"outcome": "error", "detail": "点了 PDF 链接但没有下载开始"}
    finally:
        watch.close()


JS_TF_PDF = r"""() => {
  const links = [...document.querySelectorAll('a[href]')];
  const a = links.find(x => {
    const h = x.getAttribute('href') || '';
    return /\/doi\/pdf\//.test(h) && !/downloadSupplement|downloadTable/i.test(h);
  });
  if (a) a.setAttribute('data-litdl', 'tf');
  const body = document.body ? document.body.innerText : '';
  return {
    link: a ? a.href : null,
    access: (body.match(/Open access|Get Access|Purchase PDF|Full access/i) || [''])[0],
    inst: (body.match(/Access provided by[^\n]*/i) || [''])[0],
    title: document.title
  };
}"""


def r_tandf(env, page, row, out):
    """Taylor & Francis on the campus IP: open the DOI page and follow its own 'Download PDF' link.

    The school's subscription is recognised by the publisher (the page says 'Access provided by
    <the school>'), so this works without a WebVPN entry. The link is never built
    by hand; only the one printed on the page is clicked."""
    doi = row.get("doi")
    if not doi:
        return {"outcome": "no_link", "detail": "这一行没有 DOI"}
    try:
        page.goto("https://doi.org/" + doi, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        if "Download is starting" not in str(e):
            return {"outcome": "error", "detail": str(e).splitlines()[0][:140]}
    page.wait_for_timeout(5000)
    ps = page_state(page)
    if ps["challenge"]:
        page, res = get_past(env, page, "T&F 人机验证", out)
        if res:
            return res
    info = {}
    for _ in range(4):                    # the PDF link renders a few seconds after a challenge clears
        try:
            info = page.evaluate(JS_TF_PDF)
        except Exception:
            info = {}
        if info.get("link"):
            break
        page.wait_for_timeout(3500)
    if not info.get("link"):
        acc = info.get("access") or ""
        if not info.get("inst"):
            # nobody asks the user whether they are on campus: the page says it. Without the publisher's
            # "Access provided by <school>" line this answer is about the network, not about the paper
            return {"outcome": "no_campus", "detail": "T&F 页面上没有“Access provided by …”，出版商没认出学校（不在校园网）"}
        if re.search(r"get access|purchase", acc, re.I):
            return {"outcome": "no_access", "detail": f"T&F 认出了学校（{info['inst'][:50]}），这篇只显示 {acc}，学校没订"}
        return {"outcome": "no_link", "detail": f"T&F 页面没渲染出 PDF 链接：{(info.get('title') or '')[:40]}"}
    watch = DownloadWatch(env.sess.ctx, page)
    try:
        page.locator('[data-litdl="tf"]').first.click(timeout=8000, no_wait_after=True)
        res, page = await_file(env, page, watch, out, "tandf", "T&F 的 PDF 在等人机验证")
        return res or {"outcome": "error", "detail": "点了 Download PDF 但没有下载开始"}
    finally:
        watch.close()


JS_PUB_PDF = r"""() => {
  const bad = /supplement|downloadTable|\.csv|\.zip|figshare/i;
  const links = [...document.querySelectorAll('a[href]')];
  const ok = links.filter(x => !bad.test((x.getAttribute('href') || '') + ' ' + (x.innerText || '')));
  const find = pred => ok.find(pred);
  const a = find(x => /\/doi\/pdf\//.test(x.getAttribute('href') || ''))
         || find(x => /\/insight\/content\/doi\/.*\/full\/pdf/i.test(x.getAttribute('href') || ''))
         || find(x => /\/pdfft\b/i.test(x.getAttribute('href') || ''))
         || find(x => /download.*pdf|pdf.*download/i.test(x.innerText || ''));
  if (a) a.setAttribute('data-litdl', 'pubpdf');
  const body = document.body ? document.body.innerText : '';
  return {
    link: a ? a.href : null,
    text: a ? (a.innerText || '').trim().slice(0, 40) : '',
    access: (body.match(/Open access|Get Access|Purchase PDF|Full access|Buy article/i) || [''])[0],
    inst: (body.match(/Access provided by[^\n]*|Access through[^\n]*|Institutional access[^\n]*/i) || [''])[0],
    title: document.title
  };
}"""


def r_pubdoi(env, page, row, out):
    """Publisher page on the campus IP: open the DOI, follow the PDF link printed on the page.

    Used for Emerald / SAGE (and as an explicit `--via pubdoi` fallback for others) when the school
    has IP-based access but no WebVPN portal entry. The address is never built by hand."""
    doi = row.get("doi")
    if not doi:
        return {"outcome": "no_link", "detail": "这一行没有 DOI"}
    try:
        page.goto("https://doi.org/" + doi, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        if "Download is starting" not in str(e):
            return {"outcome": "error", "detail": str(e).splitlines()[0][:140]}
    page.wait_for_timeout(5000)
    ps = page_state(page)
    if ps["challenge"]:
        page, res = get_past(env, page, "出版商人机验证", out)
        if res:
            return res
    info = {}
    for _ in range(4):
        try:
            info = page.evaluate(JS_PUB_PDF)
        except Exception:
            info = {}
        if info.get("link"):
            break
        page.wait_for_timeout(3500)
    if not info.get("link"):
        acc = info.get("access") or ""
        if not info.get("inst"):
            # see r_tandf. A school that has no deal with this publisher at all gives the same page on campus:
            # then the row is closed by hand, `litdl mark --tried "publisher=…"`, once that is known
            return {"outcome": "no_campus", "detail": "出版商页面上没有机构授权标记：不在校园网，或者学校和这家出版商没有按 IP 的授权"}
        if re.search(r"get access|purchase|buy article", acc, re.I):
            return {"outcome": "no_access", "detail": f"出版商认出了学校（{info['inst'][:50]}），这篇只显示 {acc}，学校没订"}
        return {"outcome": "no_link", "detail": f"页面没渲染出 PDF 链接：{(info.get('title') or '')[:40]}"}
    watch = DownloadWatch(env.sess.ctx, page)
    try:
        page.locator('[data-litdl="pubpdf"]').first.click(timeout=8000, no_wait_after=True)
        res, page = await_file(env, page, watch, out, "pubdoi", "出版商的 PDF 在等人机验证")
        return res or {"outcome": "error", "detail": "点了 PDF 链接但没有下载开始"}
    finally:
        watch.close()


def r_vpnpub(env, page, row, out):
    """A publisher that has an entry of its own in the school WebVPN but no recipe of its own: open the article
    through the WebVPN host (the row's webvpn_page) and follow the PDF link the page prints. Works from any
    network, unlike tandf / pubdoi. Which publishers go this way is the school's holdings table, after one
    paper each was tried: an entry on the portal does not mean the entry works (2026-11-21: Taylor & Francis
    works; Emerald stops at a security check that cannot finish behind the WebVPN)."""
    # built now, not read from the manifest: the school's host forms may have been learnt after the manifest was planned
    url = vpn_page(env, row)
    if not url:
        return {"outcome": "no_link", "detail": "这一行没有 WebVPN 页面地址"}
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            if "Download is starting" in str(e):
                break
            # 2026-11-21: the first request through this entry after a login timed out, the same one a minute
            # later took 6 s. One more try, then it is a transport error (not an answer about the paper).
            if attempt == 2 or "Timeout" not in str(e):
                return {"outcome": "error", "detail": str(e).splitlines()[0][:140]}
            page.wait_for_timeout(3000)
    page.wait_for_timeout(5000)
    ps = page_state(page)
    if ps["signin"] or "sign_in" in page.url:
        return {"outcome": "login", "detail": "WebVPN 会话失效"}
    if ps["challenge"]:
        try:
            body = (page.inner_text("body") or "")[:1500]
        except Exception:
            body = ""
        if re.search(r"无法连接到网站|安全验证过程无法完成|unable to connect to the website|cannot complete", body, re.I):
            # the check loads its script from a host the WebVPN does not rewrite: nobody can pass it, do not wait
            return {"outcome": "blocked", "detail": "这家出版商的安全验证在 WebVPN 里过不去（验证脚本连不上）"}
        page, res = get_past(env, page, "出版商人机验证", out)
        if res:
            return res
    info = {}
    for _ in range(4):                    # the PDF link renders a few seconds after the page
        try:
            info = page.evaluate(JS_PUB_PDF)
        except Exception:
            info = {}
        if info.get("link"):
            break
        page.wait_for_timeout(3500)
    if not info.get("link"):
        acc = info.get("access") or ""
        if re.search(r"get access|purchase|buy article", acc, re.I):
            return {"outcome": "no_access", "detail": f"经 WebVPN 打开只显示 {acc}，学校没订这一刊（期）"}
        return {"outcome": "no_link", "detail": f"页面没渲染出 PDF 链接：{(info.get('title') or '')[:40]}"}
    watch = DownloadWatch(env.sess.ctx, page)
    try:
        try:
            page.locator('[data-litdl="pubpdf"]').first.click(timeout=8000, no_wait_after=True)
        except Exception:
            pass
        res, page = await_file(env, page, watch, out, "vpnpub", "出版商的 PDF 在等人机验证", seconds=25)
        if res:
            return res
    finally:
        watch.close()
    # 2026-11-21: in one of three runs the click on T&F's "Download PDF" started nothing and the recipe sat out its
    # whole timeout; the same link fetched from the page itself came down at once. So: 25 s for the click, then
    # fetch the printed link on the page's own origin, then open it.
    say(f"       点链接 25 秒没有下载开始，改为在页面里直接取：{info['link'][:90]}")
    res = inpage_fetch(page, info["link"], out, env.timeout("vpnpub"))
    if res["outcome"] == "ok":
        return res
    res2 = nav_fetch(env, page, info["link"], out, env.timeout("vpnpub"), hops=0, label="出版商的 PDF 在等人机验证")
    if res2["outcome"] != "ok":
        res2["detail"] = f"点链接、页内取（{res.get('detail', '')[:40]}）、直接打开都没取到：{res2.get('detail', '')}"
    return res2


def r_public(env, page, row, out):
    """A public copy: publisher OA page, repository, SSRN. Tries the row's alt_copies, then oa_pdf, then the DOI page if OA."""
    urls = [u for u in (row.get("alt_copies") or []) if isinstance(u, str)]
    urls += [c.get("pdf") or c.get("landing") for c in (row.get("alt_copies") or []) if isinstance(c, dict)]
    if row.get("oa_pdf"):
        urls.append(row["oa_pdf"])
    if row.get("is_oa") and row.get("doi"):
        urls.append("https://doi.org/" + row["doi"])
    urls = [u for i, u in enumerate(urls) if u and u not in urls[:i]]
    if env.url:
        urls = [env.url]
    if not urls:
        return {"outcome": "no_link", "detail": "这一行没有公开版本的链接（先跑 litdl alts）"}
    res, stopped = {}, set()
    for u in urls[:4]:
        host = urllib.parse.urlsplit(u).netloc
        if host in stopped:
            continue
        say(f"       打开 {u[:100]}")
        if page is None or page.is_closed():
            page = env.sess.ctx.new_page()
        gen = env.sess.generation
        res = nav_fetch(env, page, u, out, env.timeout("public"), label=f"{host} 的人机验证")
        if env.sess.generation != gen:          # a verification was got past hands off: the old page object is dead
            page = None
        res["detail"] = f"{host}：{res.get('detail', '')}"
        if res["outcome"] in ("ok", "throttle"):
            break
        if res["outcome"] in ("need_user", "blocked"):
            # this site is stopped, the paper is not: another copy of it may sit on another site (2026-11-21 re-test:
            # CityU's copy behind a verification, PolyU's copy of the same paper came down at once). The next copy
            # opens in a tab of its own.
            stopped.add(host)
            page = None
            continue
        time.sleep(random.uniform(3, 6))
    return res


def public_host(row, env):
    c = (row.get("alt_copies") or [None])[0]
    u = env.url or (c if isinstance(c, str) else (c or {}).get("pdf") or (c or {}).get("landing")) or row.get("oa_pdf") or "doi.org"
    if "10.2139/ssrn" in u.lower() or "ssrn.com" in u.lower():
        return "papers.ssrn.com"                       # SSRN DOIs resolve to one site: pace and stop them together
    return urllib.parse.urlsplit(u).netloc or "doi.org"


JS_SCIHUB = r"""() => {
  const srcs = [...document.querySelectorAll('object[data], embed[src], iframe[src], a[href]')]
    .map(e => e.getAttribute('data') || e.getAttribute('src') || e.getAttribute('href') || '')
    .filter(u => /\.pdf(\?|#|$)/i.test(u) && !/^(javascript|mailto):/i.test(u));
  const body = document.body ? document.body.innerText : '';
  return {pdf: srcs.length ? new URL(srcs[0].split('#')[0], location.href).href : '',
          // the article page keeps a hidden altcha element (2026-11-21: waited 40 s on a page that had passed), so the
          // robot page is known by its title and by the button it shows, not by that element
          robot: /机器人|are you a robot/i.test(document.title) || !!document.querySelector('[onclick="check()"]'),
          missing: /不可用|尚未收录|not available|not (yet )?in (my|the) database|article not found|не найден|недоступна/i
                     .test(document.title + ' ' + body.slice(0, 400)),
          title: document.title, host: location.host};
}"""


def r_scihub(env, page, row, out):
    """Sci-Hub: open <mirror>/<doi>, pass its own 'are you a robot' page (one click on the page's button starts a
    proof of work, 2 s on 2026-11-21; the cookie then holds for later papers), and take the PDF the article page
    embeds from the same site. 'Not in the database' is an answer (not_found); a mirror that does not load is not."""
    doi = row.get("doi")
    if not doi:
        return {"outcome": "no_link", "detail": "这一行没有 DOI"}
    res = {"outcome": "error", "detail": "没有可用的 Sci-Hub 镜像"}
    for host in SCIHUB:
        try:
            r = page.goto(f"https://{host}/{doi}", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            res = {"outcome": "error", "detail": f"{host} 打不开：{str(e).splitlines()[0][:80]}"}
            continue
        if r is not None and r.status >= 500:
            res = {"outcome": "error", "detail": f"{host}：HTTP {r.status}"}
            continue
        page.wait_for_timeout(2500)
        info = page.evaluate(JS_SCIHUB)
        if not info["pdf"] and handsoff_ok() and (info["robot"] or page_state(page)["challenge"]):
            # like every verification, hands off (2026-11-22): the page's own "不是" button, or a Cloudflare check in
            # front of the mirror, is clicked with the system mouse while nothing is attached; the switches are the same
            page, res = get_past(env, page, f"{host} 的机器人确认", out)
            if res:
                return res
            page.wait_for_timeout(2000)
            info = page.evaluate(JS_SCIHUB)
        elif info["robot"] and not info["pdf"]:
            # the page's own button is a click on a verification like any other: it obeys --no-auto-captcha and
            # LITDL_CAPTCHA=0 (an agent's report, 2026-11-21: "the switch says always ask the user, this clicked anyway")
            auto = captcha_ai.config(HOME)["enabled"]
            if auto:
                try:
                    page.click('[onclick="check()"]', timeout=5000)
                except Exception:
                    pass
            for _ in range(40 if auto else 0):                       # the proof of work, then the page reloads into the article
                page.wait_for_timeout(1000)
                try:
                    info = page.evaluate(JS_SCIHUB)
                except Exception:
                    continue                          # navigating
                if not info["robot"]:
                    break
            if info["robot"] and not wait_human(page, "Sci-Hub 的机器人确认，请您点一下“不是”", env.human_wait, env.prog):
                return {"outcome": "need_user", "detail": f"{host} 的机器人确认没过"}
            page.wait_for_timeout(2000)
            info = page.evaluate(JS_SCIHUB)
        if info["pdf"]:
            say(f"       {info['host']} 上的 PDF：{info['pdf'][:100]}")
            res = inpage_fetch(page, info["pdf"], out, env.timeout("scihub"))
            if res["outcome"] != "ok":                # another origin, or the page moved on: open the file itself
                res = nav_fetch(env, page, info["pdf"], out, env.timeout("scihub"), hops=0, label=f"{host} 的人机验证")
            res["detail"] = f"{info['host']}：{res.get('detail', '')}"
            return res
        if info["missing"]:
            return {"outcome": "not_found", "detail": f"{info['host']} 没有收录这篇"}
        res = {"outcome": "no_link", "detail": f"{info['host']} 的页面上没有 PDF：{info['title'][:50]}"}
    return res


RECIPES = {"ebsco": r_ebsco, "wiley": r_wiley, "direct": r_direct, "elsevier": r_elsevier, "vpnpub": r_vpnpub,
           "public": r_public, "tandf": r_tandf, "pubdoi": r_pubdoi, "scihub": r_scihub}
NEEDS_VPN = litdl.VPN_RECIPES                              # tandf / pubdoi use the campus IP, not WebVPN
CAMPUS_RECIPES = litdl.CAMPUS_RECIPES                      # open the publisher's own site; need the campus IP
# what the manifest's `tried` ledger calls each channel
LEDGER = litdl.LEDGER


def pick_recipe(row, via, skip=()):
    """auto: the next database that can hold this paper and has no answer in the row's ledger yet
    (litdl.sources: the school's database for this publisher, then Sci-Hub, then public copies).
    A public copy is only 'next' when the row has a link to open; `litdl alts` finds those or closes the channel."""
    if via != "auto":
        return via
    tried = row.get("tried") or {}
    for r in litdl.sources(row):
        if r in skip or (LEDGER[r] in tried and row.get("status") == "todo"):
            continue
        if row.get("status") == "todo" and litdl.held_up(row, r):
            continue                                  # stopped at a verification page last time: on to the next database
        if r == "public" and not (row.get("alt_copies") or row.get("oa_pdf") or row.get("is_oa")):
            continue
        return r
    return None


# --------------------------------------------------------------------------- fetch: the sequential queue
# per-site rules for public copies. SSRN (2026-11-19): 8 papers in a row came down, the 9th and 10th got 403, and
# 80 minutes later the site was back at the verification page. So: a longer gap, and at most 6 per hour.
HOST_RULES = {"public:papers.ssrn.com": {"gap": 30, "cap": 6, "window": 60}}


def recent_ok(stage, key, minutes):
    """How many files this site/channel gave in the last `minutes`, counted from the stage's results.jsonl."""
    f, n, cutoff = Path(stage) / "results.jsonl", 0, time.time() - minutes * 60
    if not f.exists():
        return 0
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            x = json.loads(line)
            if x.get("key") == key and x.get("outcome") == "ok" and \
                    time.mktime(time.strptime(x["at"], "%Y-%m-%d %H:%M:%S")) >= cutoff:
                n += 1
        except Exception:
            continue
    return n


def not_final(row):
    """The row has a file, but the ingest marked it as a preprint / accepted manuscript / repository copy."""
    v = row.get("version") or ""
    return row.get("status") in ("done", "have") and bool(v) and v != "未见预印本标记"


def pdf_pages(path):
    try:
        import fitz
        d = fitz.open(path)
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 0


def detach_fetch(a, stage):
    """Start the same fetch as a process of its own and return. A shell tool's background mode ends after about ten
    minutes; a round of 40 EBSCO papers takes 50. Output goes to <stage>/fetch.log; follow it with `wait` / `progress`."""
    old = read_json(stage / "progress.json") or {}
    if old.get("state") == "running" and pid_alive(old.get("pid")):
        sys.exit(f"这个 stage 目录里已有一个 fetch 在跑（pid {old['pid']}，{old.get('updated')}）。用 progress 看进度，不要并发。")
    log = stage / "fetch.log"
    rest = [x for x in sys.argv[1:] if x != "--detach"]
    argv = [sys.executable, "-X", "utf8", "-P", str(Path(__file__).resolve()), *rest]
    mark = f"===== {now()} fetch（脱离会话）"
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log.open("ab") as f:
        f.write(("\n" + mark + "\n").encode("utf-8"))
        f.flush()
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, creationflags=flags,
                             close_fds=True, env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    t0 = time.time()
    while time.time() - t0 < 30:                 # wait until the child owns progress.json, or has already finished
        if (read_json(stage / "progress.json") or {}).get("pid") == p.pid or p.poll() is not None:
            break
        time.sleep(0.5)
    if p.poll() is not None:
        say(f"fetch 已经结束（退出码 {p.returncode}），它的输出：")
        say(log.read_text(encoding="utf-8", errors="replace").split(mark)[-1].strip()[-3000:])
        return p.returncode
    say(f"fetch 已脱离本会话在运行（pid {p.pid}），输出写到 {log}")
    say(f"等它结束或等到要用户出手：litbrowser.py wait \"{stage}\"（一次最多等 9 分钟，没结束就再跑一次）")
    say("看进度：progress；暂停：pause。它还在跑时不要关专用 Chrome。")
    return 0


def cmd_wait(a):
    """Block until the fetch in <stage> ends, needs the user, or `--timeout` seconds pass; then print the progress."""
    path, t0 = Path(a.stage) / "progress.json", time.time()
    why = f"等了 {a.timeout} 秒，fetch 还在跑：再跑一次 wait"
    while time.time() - t0 < a.timeout:
        d = read_json(path) or {}
        if not d:
            why = "这个目录里没有 progress.json"
            break
        if d.get("state") != "running" or not pid_alive(d.get("pid")):
            why = "fetch 已结束"
            break
        if d.get("need_user") and not a.ignore_need_user:
            why = "fetch 停在人机验证上等用户：现在就告诉用户；要接着等就加 --ignore-need-user 再跑 wait"
            break
        time.sleep(5)
    say(why)
    return cmd_progress(a)


class Progress:
    def __init__(self, stage, manifest, total):
        self.path = Path(stage) / "progress.json"
        self.d = {"pid": os.getpid(), "manifest": str(manifest), "state": "running", "started": now(), "updated": now(),
                  "total": total, "ok": 0, "current": None, "need_user": None, "items": [], "stopped_because": ""}
        self.save()

    def save(self):
        self.d["updated"] = now()
        write_json(self.path, self.d)

    def current(self, row, recipe):
        self.d["current"] = {"id": row["id"], "recipe": recipe, "since": now(), "title": (row.get("target") or "")[:70]}
        self.save()

    def need_user(self, why, url, tab=None):
        self.d["need_user"] = {"why": why, "url": url, "tab": tab, "since": now()} if why else None
        self.save()

    def item(self, rec):
        self.d["items"].append(rec)
        self.d["ok"] += rec["outcome"] == "ok"
        self.d["current"] = None
        self.save()

    def finish(self, state, why=""):
        self.d.update(state=state, stopped_because=why, current=None, need_user=None)
        self.save()


class Env:
    def __init__(self, sess, a, prog):
        self.sess, self.prog = sess, prog
        self.vpn, self.opid = getattr(a, "webvpn", None), getattr(a, "opid", None)
        self.human_wait, self.url = getattr(a, "human_wait", 300), getattr(a, "url", None)
        self._timeout = getattr(a, "timeout", None)
        self.passes = 0                       # verifications got past for the paper in hand (see get_past)

    def timeout(self, recipe):
        return self._timeout or FETCH_TIMEOUT[recipe]


def cmd_fetch(a):
    rows = litdl.load(a.manifest)
    want = {int(x) for x in a.ids.split(",")} if a.ids else None
    todo = []
    upgrades = []
    for r in rows:
        if want is not None and r["id"] not in want:
            continue
        if r["status"] != "todo":
            # a row that only has a preprint / repository copy may be sent to the school channels again,
            # but only when it is named with --ids and only through a formal channel
            if not (want is not None and not_final(r) and pick_recipe(r, a.via) in NEEDS_VPN):
                continue
            upgrades.append(r["id"])
        if a.bucket and r.get("bucket") not in a.bucket.split(","):
            continue
        y = r.get("year_print") or r.get("year_online") or 0
        if (a.year_max and y and y > a.year_max) or (a.year_min and y and y < a.year_min):
            continue
        rec = pick_recipe(r, a.via)
        if rec:
            todo.append((r, rec))
    stage = Path(a.stage)
    stage.mkdir(parents=True, exist_ok=True)
    if a.dry_run:
        for r, rec in todo:
            say(f"{r['id']:>4} {rec:9s} {r.get('bucket', ''):9s} {(r.get('target') or r.get('doi') or '')[:80]}"
                + (f"  [已有{r.get('version')}，找正式版]" if r["id"] in upgrades else ""))
        say(f"共 {len(todo)} 篇（--limit {a.limit} 生效后最多 {min(len(todo), a.limit or len(todo))} 篇）")
        return 0
    if a.detach:
        return detach_fetch(a, stage)
    old = read_json(stage / "progress.json") or {}
    if old.get("state") == "running" and pid_alive(old.get("pid")) and old.get("pid") != os.getpid():
        sys.exit(f"这个 stage 目录里已有一个 fetch 在跑（pid {old['pid']}，{old.get('updated')}）。用 progress 看进度，不要并发。")
    (stage / "HALT").unlink(missing_ok=True)
    if not todo:
        say("没有符合条件的待下载行")
        return 0
    capped = todo[:a.limit] if a.limit else todo
    if any(rec in NEEDS_VPN for _, rec in capped):    # the user's own school, filled in once; nothing is assumed
        a.webvpn = litdl.need_webvpn(a.webvpn)
        if not a.webvpn:
            sys.exit("配置里写的是没有学校 WebVPN（none），而这一轮点了经 WebVPN 的库。去掉 --via，让脚本自己排。")
    if any(rec == "ebsco" for _, rec in capped) and not a.opid:
        sys.exit("NEED_USER  还没填 EBSCO 的 opid。用户登录 WebVPN 后，从门户点开 EBSCO，地址是 "
                 "https://research-ebsco-com-443.<vpn>/c/<opid>/search，中间那段就是；"
                 "然后跑：litdl.py config --ebsco-opid <opid>（见 references/setup.md）。")
    prog = Progress(stage, a.manifest, len(capped))
    results = (stage / "results.jsonl").open("a", encoding="utf-8")
    with Session(exe=a.exe) as sess:
        env = Env(sess, a, prog)
        if any(rec in NEEDS_VPN for _, rec in capped) and not vpn_logged_in(sess, a.webvpn):   # before the first request
            page = sess.ctx.new_page()
            page.goto(f"https://{a.webvpn}/", wait_until="domcontentloaded")
            page.bring_to_front()
            prog.finish("stopped", "WebVPN 没登录")
            say(f"NEED_USER  学校 WebVPN 没登录。登录页已在专用 Chrome 里打开，请用户登录后重跑（或先跑 login webvpn --wait）。")
            return 2
        pacers, blocked_hosts, why = {}, set(), ""
        off_campus = set()                             # campus-IP recipes the publisher answered "who are you" to in this run
        page = sess.ctx.new_page()
        n_done = 0
        for i, (row, rec) in enumerate(capped):
            if (stage / "HALT").exists():
                why = "收到暂停请求"
                break
            if rec in off_campus:                      # not on campus: go on to the next database that can hold it
                rec = pick_recipe(row, a.via, off_campus) if a.via == "auto" else None
                if not rec:
                    continue
            out = stage / f"{row['id']}.pdf"
            if out.exists() and not pdf_problem(out):
                say(f"{row['id']:>4} {rec:9s} SKIP 已在临时目录")
                prog.d["total"] -= 1
                continue
            # WebVPN channels share the school's exit, so each has one pacer; public copies sit on unrelated sites: one per host
            key = rec if rec != "public" else "public:" + public_host(row, env)
            if rec == "vpnpub":                        # one recipe, several publishers: stop and pace each on its own
                key = "vpnpub:" + (urllib.parse.urlsplit(vpn_page(env, row)).hostname or "")
            if key in blocked_hosts:                   # this channel / site was stopped earlier in the run
                continue
            rule = HOST_RULES.get(key, {})
            if rule and recent_ok(stage, key, rule["window"]) >= rule["cap"]:
                blocked_hosts.add(key)
                why = f"{key}：{rule['window']} 分钟内已下到 {rule['cap']} 篇，到这个站点的一轮上限，歇够了再跑"
                say(f"{row['id']:>4} {rec:9s} CAP           {why}")
                continue
            pacer = pacers.setdefault(key, litdl.Pacer(a.delay or rule.get("gap") or GAPS[rec], a.max_delay,
                                                       a.rest_every, a.stop_after))
            halted = (stage / "HALT").exists
            if n_done and pacer.n:
                pacer.wait(stop=halted)
            elif n_done:                               # first request to this host in this run: plain base gap
                t = pacer.base * random.uniform(0.7, 1.6)
                say(f"       wait {t:.0f}s")
                end = time.time() + t
                while time.time() < end and not halted():
                    time.sleep(1)
            if halted():                               # a pause during the wait: no new request
                why = "收到暂停请求"
                break
            prog.current(row, rec)
            t0 = time.time()
            gen = sess.generation
            env.passes = 0
            try:
                if page.is_closed():
                    page = sess.ctx.new_page()
            except Exception:
                pass
            tabs_before, mine = tab_ids(), sess.target_id(page)
            try:
                res = RECIPES[rec](env, page, row, out)
            except Exception as e:
                res = {"outcome": "error", "detail": str(e).splitlines()[0][:160]}
            lost = ""
            if sess.generation != gen:                 # the recipe let go of the browser: page objects from before are dead
                try:
                    sess.reattach()
                    page = sess.ctx.new_page()
                    # the tabs this paper used (its own, the ones reopened hands off) are done: close them, keep one
                    fresh = sess.target_id(page)
                    for tid in (tab_ids() - tabs_before) | {mine}:
                        if tid and tid != fresh:
                            close_tab(tid)
                except Exception as e:
                    lost = f"松开浏览器后接不回去：{str(e).splitlines()[0][:100]}"
            n_done += 1
            sec = round(time.time() - t0)
            oc = res["outcome"]
            if oc == "ok" and 0 < pdf_pages(out) <= 2:
                res["detail"] = (res.get("detail", "") + "；只有 1–2 页，多半是摘要页或补充材料，入库会报 SUSPECT，看过首页再定")[:200]
            line = {"id": row["id"], "recipe": rec, "channel": LEDGER[rec], "key": key, "outcome": oc, "detail": res.get("detail", ""),
                    "size": res.get("size"), "sec": sec, "at": now()}
            results.write(json.dumps(line, ensure_ascii=False) + "\n")
            results.flush()
            prog.item(line)
            say(f"{row['id']:>4} {rec:9s} {oc.upper():13s} {sec:>4}s {(res.get('size') or 0) // 1024:>6} KB  {res.get('detail', '')[:90]}")
            if lost:
                why = lost
                break
            tidy(sess, page)
            if oc == "login":
                why = "WebVPN 会话失效，请用户重新登录（login webvpn --wait）"
                break
            if oc == "no_campus":                      # an answer about the network, not about the paper
                off_campus.add(rec)
                say(f"       不在校园网：这一轮不再走 {rec}（靠校园 IP 的配方），这几行改走下一个库；回到校园网后重跑才算试过")
                nxt = pick_recipe(row, a.via, off_campus) if a.via == "auto" else None
                if nxt:
                    capped.append((row, nxt))
                    prog.d["total"] += 1
                continue
            if oc in ("need_user", "blocked"):       # stop this channel, do not refresh, do not retry
                if oc == "need_user":                # leave the verification page as it is for the user
                    page = sess.ctx.new_page()
                blocked_hosts.add(key)               # for public copies only this one site is stopped, other sites go on
                why = f"{key}：{res.get('detail', '')}"
                if rec in CAMPUS_RECIPES and a.via == "auto":
                    # off campus the publisher's own site often answers with a verification page instead of
                    # "who are you" (2026-11-21, foreign exit): same meaning for the queue, go on to the next database
                    off_campus.add(rec)
                    nxt = pick_recipe(row, a.via, off_campus)
                    if nxt:
                        say(f"       {rec} 停在验证页上：这几行这一轮改走下一个库（{nxt} 起），publisher 一栏留着没有定论")
                        capped.append((row, nxt))
                        prog.d["total"] += 1
                continue
            # "not found / citation only / no access" are answers, not failures; only transport trouble slows the pace
            pacer.record(oc not in ("throttle", "error", "truncated", "not_pdf"), hard=oc == "throttle")
            if pacer.tripped:
                blocked_hosts.add(key)
                why = f"{key} 连续失败，已停这一桶（至少等 15 分钟，先用 --ids 单篇试探）"
        left = [r["id"] for r, rec in capped if not (stage / f"{r['id']}.pdf").exists()
                and r["id"] not in {x["id"] for x in prog.d["items"]}]
        prog.finish("stopped" if why else "done", why)
        if sess.ctx is not None:
            if not page.is_closed() and len(sess.ctx.pages) > 1:
                page.close()
            tidy(sess)
    results.close()
    more = len(todo) - len(capped)
    say(f"\n{prog.d['ok']} 篇到手，结果记在 {stage / 'results.jsonl'}"
        + (f"；停止原因：{why}" if why else "")
        + (f"；没有请求的序号：{','.join(map(str, left))}" if left else "")
        + (f"；另有 {more} 篇超出本轮上限（--limit {a.limit}），换别的桶或歇 20-30 分钟再跑" if more > 0 else ""))
    got_up = [i for i in upgrades if (stage / f"{i}.pdf").exists()]
    if got_up:
        say(f"这几行原来只有非正式版，这轮下到了学校渠道的版本：{','.join(map(str, got_up))}。入库时加 --replace 才会换掉旧文件。")
    say(f"下一步：litdl ingest <manifest> --src {stage} --dest <库> --move")
    return 0


def cmd_get(a):
    if re.match(r"^[A-Za-z]:[\/]", a.url):          # Git Bash rewrites an argument like /doi/pdf/x into C:/Program Files/Git/doi/pdf/x
        sys.exit(f"--url 被 shell 改写成了本地路径（{a.url[:60]}）。写完整的 https:// 网址。")
    stage = Path(a.stage)
    stage.mkdir(parents=True, exist_ok=True)
    out = stage / f"{a.id}.pdf"
    with Session(exe=a.exe) as sess:
        page = sess.find(a.tab) if a.tab else sess.ctx.new_page()
        env = Env(sess, a, None)
        env.url = None
        gen, tabs_before, mine = sess.generation, tab_ids(), sess.target_id(page)
        if a.inpage:
            res = inpage_fetch(page, a.url, out, a.timeout or 240)
        else:
            res = nav_fetch(env, page, a.url, out, a.timeout or 240)
        if sess.generation != gen:                    # a verification was got past hands off: tidy up its tabs
            sess.reattach()
            keep = sess.ctx.new_page()
            fresh = sess.target_id(keep)
            for tid in (tab_ids() - tabs_before) | ({mine} if not a.tab else set()):
                if tid and tid != fresh:
                    close_tab(tid)
        elif not a.tab and res["outcome"] != "need_user" and len(sess.ctx.pages) > 1:
            page.close()                              # a verification page stays open for the user
        tidy(sess)
    if not a.channel:                                 # 2026-11-21: a Springer PDF taken by hand went into the ledger as
        host = urllib.parse.urlsplit(a.url).hostname or ""      # "public" and was labelled "not the publisher's version"
        vpn = (a.webvpn or "").lower()
        a.channel = ("scihub" if "sci-hub" in host or "sci-net" in host
                     else "publisher" if vpn and vpn not in litdl.NO_VPN and host.endswith("." + vpn) else "public")
    line = {"id": a.id, "recipe": "get", "channel": a.channel, "outcome": res["outcome"],
            "detail": f"{urllib.parse.urlsplit(a.url).netloc}：{res.get('detail', '')}", "size": res.get("size"), "at": now()}
    with (stage / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    say(f"{a.id:>4} get       {res['outcome'].upper():13s} {(res.get('size') or 0) // 1024:>6} KB  {res.get('detail', '')[:100]}")
    return 0 if res["outcome"] == "ok" else 1


def cmd_solve(a):
    """One attempt at the verification on one tab, with nothing attached (like every verification since 2026-11-22):
    a known checkbox or the page's own button with the system mouse, otherwise a click / slider plan from the vision
    model. Then HANDSOFF_AFTER seconds to see whether the page moved on. Does nothing else."""
    if a.attempts:
        captcha_ai.set_attempts(a.attempts)
    cfg = captcha_ai.config(HOME)
    if not cfg["ready"]:
        say("没配视觉模型：只尝试点已知的验证复选框和 Sci-Hub 的“不是”按钮；"
            "点选和滑块要在 ~/.litdl/.env 里配 LITDL_CAPTCHA_API_KEY（或 OPENAI_API_KEY）。")
    if not handsoff_ok():                         # LITDL_HANDSOFF=0 or no websockets: the older attached attempt
        with Session(autostart=False) as sess:
            page = sess.find(a.tab)
            ok = captcha_ai.solve(page, "手动过验证", state_fn=page_state, home=HOME, log=say)
            say("通过" if ok else "没通过，页面留着请用户点")
        return 0 if ok else 1
    import osmouse
    if not osmouse.available():
        sys.exit("不挂浏览器的点击要用系统鼠标，只在 Windows 上可用：请用户自己点")
    t = bare_find(a.tab)
    tid, ws, title = t["id"], t["webSocketDebuggerUrl"], t.get("title") or ""
    devtools(f"/json/activate/{tid}")
    time.sleep(0.4)
    look = bare_look(ws)
    hits, vw, vh, _ = look
    if not bare_challenge(title, look):
        say(f"这一页看起来不是验证页（{title[:40]!r}），什么都没点")
        return 0
    pid = bare_browser_pid()
    hwnd = osmouse.window_for(pid, title)
    if hits:
        x, y = hit_point(hits[0])
        ok = bool(hwnd) and osmouse.click_css(hwnd, vw, vh, x, y)
        say(f"{hits[0]['kind']}：系统鼠标点 ({x:.0f},{y:.0f})" if ok else "专用 Chrome 的窗口切不到前台，没点")
    elif cfg["ready"]:
        ok = handsoff_model(tid, ws, pid, title, t.get("url"), look, cfg)
    else:
        say("页面上没有已知的验证控件，也没配视觉模型：请用户点")
        return 1
    if not ok:
        return 1
    time.sleep(HANDSOFF_AFTER)
    info = tab_info(tid)
    try:
        passed = info is None or not bare_challenge(info.get("title") or "", bare_look(info["webSocketDebuggerUrl"]))
    except Exception:
        passed = False
    say("通过" if passed else "没通过，页面留着请用户点")
    return 0 if passed else 1


def cmd_look(a):
    """What a tab shows, read with nothing attached: title, address, viewport, known verification controls (with the
    point a click goes to), start of its text; with --out also a screenshot. Safe while a verification page is up."""
    t = bare_find(a.tab)
    ws = t["webSocketDebuggerUrl"]
    look = bare_look(ws)
    hits, vw, vh, text = look
    rec = {"tab": t["id"][:6], "title": t.get("title"), "url": t.get("url"), "viewport": [vw, vh],
           "verification": bare_challenge(t.get("title") or "", look),
           "controls": [{**{k: round(v) if isinstance(v, float) else v for k, v in h.items()},
                         "click": [round(c) for c in hit_point(h)]} for h in hits],
           "text": text[:a.max]}
    if a.out:
        devtools(f"/json/activate/{t['id']}")         # a tab behind others does not paint
        time.sleep(0.4)
        raw, _, _ = bare_shot(ws, fmt="png")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_bytes(raw)
        iw = (captcha_ai._image_size(raw) or (0, 0))[0]
        # the image is in device pixels; press takes CSS pixels
        rec.update(shot=a.out, image_px_per_css=round(iw / vw, 3) if vw else None)
    say(json.dumps(rec, ensure_ascii=False))
    return 0


def cmd_press(a):
    """Click one CSS-pixel point of a tab with the system mouse (with --to-x/--to-y: drag; with --hover: only move
    there), with nothing attached. For verification pages, after `look`. Windows only."""
    import osmouse
    if not osmouse.available():
        sys.exit("press 用系统鼠标，只在 Windows 上可用")
    t = bare_find(a.tab)
    devtools(f"/json/activate/{t['id']}")
    time.sleep(0.4)
    with BareTab(t["webSocketDebuggerUrl"]) as b:
        vw, vh = bare_viewport(b)
    title = (tab_info(t["id"]) or t).get("title") or ""
    hwnd = osmouse.window_for(bare_browser_pid(), title)
    if not hwnd:
        sys.exit(f"找不到显示这个标签页的专用 Chrome 窗口（找的是 {title[:30]!r}），没点")
    if a.to_x is not None and a.to_y is not None:
        ok = osmouse.drag_css(hwnd, vw, vh, a.x, a.y, a.to_x, a.to_y, duration=a.duration or random.uniform(0.65, 0.95))
        what = f"拖 ({a.x},{a.y}) -> ({a.to_x},{a.to_y})"
    elif a.hover:
        ok, what = osmouse.hover_css(hwnd, vw, vh, a.x, a.y), f"悬停 ({a.x},{a.y})"
    else:
        ok, what = osmouse.click_css(hwnd, vw, vh, a.x, a.y), f"点 ({a.x},{a.y})"
    if not ok:
        say("专用 Chrome 切不到前台，那个位置也被别的窗口盖着：没做")
        return 1
    time.sleep(a.wait)
    info = tab_info(t["id"]) or {}
    say(f"{what}  " + json.dumps({"tab": t["id"][:6], "title": info.get("title"), "url": (info.get("url") or "")[:120],
                                   "verification_title": bool(CHALLENGE_TITLE.search(info.get("title") or ""))},
                                  ensure_ascii=False))
    return 0


def cmd_progress(a):
    d = read_json(Path(a.stage) / "progress.json")
    if not d:
        say("这个目录里没有 progress.json（fetch 还没跑过）")
        return 1
    alive = pid_alive(d.get("pid"))
    state = d["state"] if d["state"] != "running" or alive else "进程已不在（异常退出）"
    say(f"状态：{state}　开始 {d['started']}　最近更新 {d['updated']}")
    say(f"已处理 {len(d['items'])}/{d['total']}，到手 {d['ok']} 篇")
    if d.get("current") and alive:
        c = d["current"]
        say(f"正在下：#{c['id']}（{c['recipe']}，自 {c['since']}）{c['title']}")
    if d.get("need_user"):
        nu = d["need_user"]
        say(f"等用户：{nu['why']}　{(nu.get('url') or '')[:90]}"
            + (f"　标签页 {nu['tab']}（浏览器已松开：要看这一页用 look，不要跑 shot、goto、status 这类会挂上浏览器的命令）"
               if nu.get("tab") else ""))
    if d.get("stopped_because"):
        say(f"停止原因：{d['stopped_because']}")
    from collections import Counter
    c = Counter(x["outcome"] for x in d["items"])
    if c:
        say("结果：" + "，".join(f"{k} {v}" for k, v in c.most_common()))
    for x in d["items"][-a.last:]:
        say(f"  #{x['id']:<4} {x['recipe']:9s} {x['outcome']:13s} {x.get('sec', '')}s  {x.get('detail', '')[:80]}")
    return 0


def cmd_pause(a):
    d = read_json(Path(a.stage) / "progress.json") or {}
    (Path(a.stage) / "HALT").write_text(now(), encoding="utf-8")
    if d.get("state") == "running" and pid_alive(d.get("pid")):
        say("已请求暂停：正在下的这一篇做完就停，不再发新请求。用 progress 确认状态变成 stopped 后再做别的。")
    else:
        say("没有正在跑的 fetch；HALT 标记已放下，下次 fetch 启动时会清掉。")
    return 0


# --------------------------------------------------------------------------- open / status / login / quit
LOGIN_PAGES = {
    "webvpn": lambda a: f"https://{a.webvpn}/",
    "ablesci": lambda a: "https://www.ablesci.com/site/login",
}


def cmd_open(a):
    started = start_chrome(a.exe)
    v = cdp_version() or {}
    say(f"专用 Chrome {'已启动' if started else '本来就在运行'}：{v.get('Browser', '?')}，端口 {PORT}，profile {PROFILE}")
    if started:
        say("出口：" + ("直连（不走系统代理）" if CHROME_DIRECT else "走系统代理（--use-proxy）"))
    else:
        with Session(autostart=False) as sess:
            if chrome_direct(sess) is False:
                say("注意：这个专用 Chrome 是走系统代理启动的。要直连：没有下载任务时 quit 再 open（WebVPN 要用户重新登录）。")
    if a.url:
        with Session(exe=a.exe) as sess:
            for u in a.url:
                p = sess.ctx.new_page()
                p.goto(u, wait_until="domcontentloaded")
                say(f"  打开 {u}")
            p.bring_to_front()
    return 0


def cmd_status(a):
    v = cdp_version()
    if not v:
        say("专用 Chrome 没在运行（litbrowser open 启动）")
        return 1
    say(f"专用 Chrome：{v.get('Browser')}，端口 {PORT}")
    say(captcha_ai.describe(HOME))
    with Session(autostart=False) as sess:
        d = chrome_direct(sess)
        say("出口：" + {True: "直连（不走系统代理）", None: "没查出来它启动时带没带 --no-proxy-server",
                      False: "走系统代理（开着 Clash 的话就是 Clash）。要直连：没有下载任务时 quit 再 open，"
                             "WebVPN 要用户重新登录"}[d])
        if not a.webvpn:
            say("WebVPN：还没填学校的地址。问用户学校 WebVPN 门户的域名，然后跑 litdl.py config --webvpn <域名>")
        elif a.webvpn.lower() in litdl.NO_VPN:
            say("WebVPN：配置里写的是没有（none）")
        elif not a.no_vpn:
            ok = vpn_logged_in(sess, a.webvpn)
            say(f"WebVPN（{a.webvpn}）：{'已登录' if ok else '没登录'}")
        for tid, p in sess.tabs():
            st = page_state(p) if p.url.startswith("http") else {"challenge": False, "blocked": False}
            flag = " [等人机验证]" if st["challenge"] else (" [被限制]" if st["blocked"] else "")
            say(f"  {tid}  {safe_title(p)[:40]!r:44s} {p.url[:80]}{flag}")
    return 0


def cmd_login(a):
    if a.site == "webvpn":
        a.webvpn = litdl.need_webvpn(a.webvpn)
        if not a.webvpn:
            sys.exit("配置里写的是没有学校 WebVPN（none）。")
    url = LOGIN_PAGES[a.site](a)
    with Session(exe=a.exe) as sess:
        if a.site == "webvpn" and vpn_logged_in(sess, a.webvpn):
            say("WebVPN 已登录，不用再登")
            return 0
        page = sess.ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()
        say(f"NEED_USER  登录页已打开并置前：{url}　请用户在专用 Chrome 里登录（账号、密码、短信验证码都由用户填）。")
        if not a.wait:
            return 2
        t0 = time.time()
        while time.time() - t0 < a.wait:          # only looks; does not open tabs or touch the page while the user types
            page.wait_for_timeout(4000)
            try:
                if a.site == "webvpn":
                    done = "/users/sign_in" not in page.url and "sign_out" in page.content()
                else:
                    done = "/site/login" not in page.url
            except Exception:
                done = False
            if done:
                say(f"{a.site} 登录成功")
                return 0
        say(f"等了 {a.wait} 秒还没登录；页面留着，用户登好后说一声")
        return 2


def cmd_quit(a):
    if not cdp_version():
        say("专用 Chrome 本来就没在运行")
        return 0
    running = [p for p in HOME.glob("**/progress.json")] if a.stage is None else [Path(a.stage) / "progress.json"]
    for p in running:
        d = read_json(p) or {}
        if d.get("state") == "running" and pid_alive(d.get("pid")) and not a.force:
            sys.exit(f"有 fetch 正在跑（{p}）。先 pause 并等它停下；确实要关再加 --force。")
    with Session(autostart=False) as sess:
        s = sess.browser.new_browser_cdp_session()
        s.send("Browser.close")
    say("专用 Chrome 已关闭（WebVPN 会话随之失效，下次要重新登录）")
    return 0


# --------------------------------------------------------------------------- portal: what the school's WebVPN offers
# database -> (hosts it lives on, words its portal entry is usually labelled with, the litdl bucket it serves)
PORTAL_DBS = {
    "ebsco": (["research.ebsco.com", "search.ebscohost.com", "web.p.ebscohost.com", "web.s.ebscohost.com"], r"ebsco", ""),
    "wiley": (["onlinelibrary.wiley.com"], r"wiley", "wiley"),
    "springer": (["link.springer.com"], r"springer", "springer"),
    "ieee": (["ieeexplore.ieee.org"], r"\bieee\b|iel\b", "ieee"),
    "elsevier": (["www.sciencedirect.com"], r"elsevier|sciencedirect|science\s*direct", "elsevier"),
    "tandf": (["www.tandfonline.com"], r"taylor|t\s*&\s*f\b|tandf", "tandf"),
    "sage": (["journals.sagepub.com"], r"\bsage\b", "sage"),
    "emerald": (["www.emerald.com"], r"emerald", "emerald"),
    "informs": (["pubsonline.informs.org"], r"informs", "informs"),
    "acm": (["dl.acm.org"], r"\bacm\b", "acm"),
    "jstor": (["www.jstor.org"], r"jstor", ""),
    "proquest": (["www.proquest.com"], r"proquest|pqdt", ""),
    "wos": (["www.webofscience.com", "webofknowledge.com"], r"web of science|\bsci\b|\bssci\b", ""),
    "cnki": (["www.cnki.net", "kns.cnki.net"], r"cnki|知网", ""),
    "wanfang": (["www.wanfangdata.com.cn"], r"万方|wanfang", ""),
}
JS_LINKS = r"""() => {
  const out = [], seen = new Set();
  const add = (raw, t) => {
    let u;
    try { u = new URL(raw, location.href).href; } catch (e) { return; }
    if (!/^https?:/.test(u) || seen.has(u + '|' + t)) return;
    seen.add(u + '|' + t);
    // `raw` as written in the page: URL() drops an explicit :443, and some portals tell sites apart by it
    out.push({url: u, raw: raw.trim().slice(0, 300), text: t.replace(/\s+/g, ' ').trim().slice(0, 80)});
  };
  for (const e of document.querySelectorAll('a[href], [data-url], [data-href], [data-link], [onclick]')) {
    const t = (e.innerText || e.getAttribute('title') || e.getAttribute('alt') ||
               (e.querySelector('img') ? e.querySelector('img').getAttribute('alt') : '') || '');
    for (const k of ['href', 'data-url', 'data-href', 'data-link']) if (e.getAttribute(k)) add(e.getAttribute(k), t);
    const m = (e.getAttribute('onclick') || '').match(/https?:\/\/[^'"\s)]+/);
    if (m) add(m[0], t);
  }
  return {url: location.href, title: document.title, links: out.slice(0, 1500)};
}"""


# Library sites built on the 汇文 ERM resource navigator keep their list in a Vue instance, each entry with a
# package_status: 1 bought, 2 on trial, 4 free. Other sites return nothing here and are read by their links alone.
JS_ERM = r"""() => {
  const d = window.vm1 && window.vm1.$data;
  if (!d || !(d.zh_listData || d.en_listData)) return [];
  const name = {1: '已购', 2: '试用', 4: '免费'};
  return [...(d.zh_listData || []), ...(d.en_listData || [])].filter(x => x && x.package_name).map(x => ({
    name: String(x.package_name).slice(0, 120), url: x.access_link || '',
    status: name[x.package_status] || ('状态' + x.package_status)}));
}"""


def portal_scan(links, vpn):
    """-> {db: {text, url, form}} for every known database a link on the page leads to. A link counts when its
    host is the database's host as this WebVPN rewrites it, or (portals that hide the target behind an opaque
    address) when the link's label names the database. `form` is how this portal writes the host, when readable."""
    found = {}
    for ln in links:
        u = urllib.parse.urlsplit(ln["url"])
        host, text = (u.hostname or "").lower(), ln["text"]
        left = host[:-len(vpn) - 1] if vpn and host.endswith("." + vpn) else ""
        pm = re.match(r"(?:https?:)?//[^/?#]+?:(\d+)(?:[/?#]|$)", ln.get("raw") or "")
        port = pm.group(1) if pm else ""
        for db, (hosts, words, _) in PORTAL_DBS.items():
            form = ""
            for h in hosts:
                dash = h.replace(".", "-")
                if left and (left == dash or left.startswith(dash + "-")):
                    form = left + ".{vpn}" + (f":{port}" if port else "")
                    orig = h
                    break
                if host == h:                         # the portal links straight to the site (campus-IP access)
                    form, orig = "直接链到官网", h
                    break
            if not form and not (text and re.search(words, text, re.I)):
                continue
            if db not in found or (form and not found[db].get("form")):
                found[db] = {"text": text, "url": ln["url"], "form": form, "host": orig if form else "",
                             "seen": left or host}        # what the link's host looks like when the name alone matched
    return found


def cmd_portal(a):
    """After the user has logged in: read the WebVPN portal's own page and list the databases it offers - which
    publishers have an entry, how this portal writes their hosts, the EBSCO customer id. When the page shows no
    database, nothing else is opened: the user is asked where the list is, and that page is read next
    (--url, or --tab for a page the user opened). User, 2026-11-21: 先直接获取 webvpn 的页面，首页没有就问用户在哪。"""
    vpn = litdl.need_webvpn(a.webvpn)
    if a.library and not (a.url or a.tab):
        # The way to a library's resource list differs from school to school, so only schools somebody has
        # actually walked are in scripts/schools.json; for every other school this is a hint, not a path.
        sch = (read_json(HERE / "schools.json", {}) or {}).get(vpn or "")
        if not sch:
            say("NEED_USER  这所学校的图书馆路径还没有记录（scripts/schools.json 里没有它）。提示：很多学校图书馆网站的"
                "“电子资源 / 数据库导航”一页比 WebVPN 首页全，还会单列免费资源和试用资源。问用户这一页在哪"
                "（图书馆网站的哪个栏目或网址），用户说了再读：litbrowser.py portal --url <网址> --save，"
                "或者用户点到那一页后 portal --tab <标签页> --save。")
            return 2
        say(f"图书馆路径（{sch.get('school', vpn)}）：{sch.get('path', '')}")
        a.url = sch["resources"].replace("{vpn}", vpn)
    with Session(exe=a.exe) as sess:
        if a.tab:
            page, mine = sess.find(a.tab), False
        else:
            if not a.url:
                if not vpn:
                    sys.exit("配置里写的是没有学校 WebVPN（none），没有门户可看。")
                if not vpn_logged_in(sess, vpn):
                    p = sess.ctx.new_page()
                    p.goto(f"https://{vpn}/", wait_until="domcontentloaded")
                    p.bring_to_front()
                    say("NEED_USER  学校 WebVPN 没登录。登录页已在专用 Chrome 里打开，请用户登录后重跑。")
                    return 2
            page, mine = sess.ctx.new_page(), True
            try:
                page.goto(a.url or f"https://{vpn}/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                say(f"页面打不开：{str(e).splitlines()[0][:120]}")
                return 1
        page.wait_for_timeout(int(a.wait * 1000))
        info = page.evaluate(JS_LINKS)
        try:
            res = page.evaluate(JS_ERM) or []         # a resource list that says what is bought / on trial / free
        except Exception:
            res = []
        found = portal_scan(info["links"] + [{"url": r["url"], "raw": r["url"], "text": r["name"]} for r in res if r["url"]], vpn)
        say(f"读的是：{info['title'][:50]!r}  {info['url'][:100]}  （{len(info['links'])} 个链接）")
        if not found:
            nav = [ln for ln in info["links"] if re.search(r"资源|数据库|外文|中文|电子|图书馆|database|resource|library", ln["text"], re.I)]
            say("NEED_USER  这一页上没有找到数据库的入口。不再自己往下翻：问用户“学校的数据库列表在哪一页”"
                "（门户里的哪个栏目，或者图书馆网站的哪个网址），用户说了位置再读那一页：")
            say("  litbrowser.py portal --url <用户给的网址>      或者用户在专用 Chrome 里点到那一页后：portal --tab <标签页>")
            if nav:
                say("  这一页上名字像入口的链接（给用户辨认用，没有打开）：")
                for ln in nav[:12]:
                    say(f"    {ln['text'][:30]}  {ln['url'][:90]}")
            if mine:
                page.bring_to_front()                 # leave it showing, so the user can point at the right place
            return 2
        special, opid = {}, ""
        say(f"找到 {len(found)} 个数据库的入口：")
        for db, f in found.items():
            note = f["form"] or f"按入口的名字认出来的；链接的主机是 {f['seen'][:50]}，不在已知的域名里"
            if f["host"] and "{vpn}" in f["form"]:
                usual = f["host"].replace(".", "-") + "-443.{vpn}"
                if f["form"] != usual:
                    special[f["host"]] = f["form"]
                    note += f"  ← 不是常规写法 {usual}"
            say(f"  {db:9s} {f['text'][:24]!r:28s} {note}")
            m = re.search(r"/c/([A-Za-z0-9]+)/", f["url"]) if db == "ebsco" else None
            opid = m.group(1) if m else opid
        if "ebsco" in found and not opid and found["ebsco"]["form"] and not a.no_follow:
            p2 = sess.ctx.new_page()                  # the entry redirects to /c/<opid>/search: read it off the address
            try:
                p2.goto(found["ebsco"]["url"], wait_until="domcontentloaded", timeout=60000)
                for _ in range(15):
                    m = re.search(r"/c/([A-Za-z0-9]+)/", p2.url)
                    if m:
                        opid = m.group(1)
                        break
                    p2.wait_for_timeout(1500)
            except Exception:
                pass
            finally:
                p2.close()
        if "ebsco" in found:
            say("  EBSCO 的 opid：" + (opid if opid else f"这一页上读不出来，沿用已经记下的 {litdl.EBSCO_OPID}" if litdl.EBSCO_OPID
                                     else "读不出来（点开 EBSCO，看地址里 /c/<opid>/search 那一段，再 litdl.py config --ebsco-opid）"))
        pubs = {b: db for db, (_, _, b) in PORTAL_DBS.items() if b}
        have = sorted(b for b, db in pubs.items() if db in found and "{vpn}" in found[db]["form"])
        named = sorted(b for b, db in pubs.items() if db in found and "{vpn}" not in found[db]["form"])
        none = sorted(b for b, db in pubs.items() if db not in found)
        say(f"有 WebVPN 入口的出版商：{'、'.join(have) or '无'}")
        if named:
            say(f"只是名字出现在这一页上、没有读到 WebVPN 入口的：{'、'.join(named)}（可能是别的库的说明里提到它，或者链到了校外）")
        say(f"这一页上没看到的：{'、'.join(none) or '无'}（不等于学校没订：可能在别的栏目，可能按校园 IP 授权，"
            "也可能全文在 EBSCO 这类集成库里。拿不准就问用户）")
        table = litdl.holdings()["bucket"]
        untried = [b for b in have if b in ("tandf", "emerald", "sage", "acm") and "vpnpub" not in table.get(b, [])]
        if untried:
            say(f"有 WebVPN 入口、收录表里还没排“出版商的 WebVPN 入口”（vpnpub）的：{'、'.join(untried)}。入口在不等于能用："
                "各拿一篇 fetch --via vpnpub --ids <序号> 试，取到了再在 ~/.litdl/holdings.json 里把 vpnpub 排到这家的最前面。")
        if res:
            from collections import Counter
            say("这一页自己标了每个库的状态：" + "、".join(f"{k} {v}" for k, v in Counter(r["status"] for r in res).most_common()))
            free = [r for r in res if r["status"] == "免费"]
            say(f"免费资源 {len(free)} 个（学校没花钱、谁都能用或全国开通的；订购的库里没有时，到这里面找对口的）：")
            for r in free:
                say(f"    {r['name'][:46]}  {r['url'][:80]}")
        if a.save:
            hf, pf = HOME / "holdings.json", HOME / "portal.json"
            h, old = read_json(hf, {}) or {}, read_json(pf, {}) or {}
            kept = {k: v for k, v in special.items() if k in h.get("webvpn_hosts", {}) and h["webvpn_hosts"][k] != v}
            new = {k: v for k, v in special.items() if k not in h.get("webvpn_hosts", {})}
            h.setdefault("webvpn_hosts", {}).update(new)     # a form already on record is known to work: it stays
            HOME.mkdir(parents=True, exist_ok=True)
            write_json(hf, h)
            merged = dict(old.get("found") or {})
            merged.update({k: v for k, v in found.items() if v.get("form") or k not in merged})
            write_json(pf, {"at": now(),
                            "pages": sorted(set((old.get("pages") or []) + ([old["page"]] if old.get("page") else []) + [info["url"]])),
                            "found": merged, "ebsco_opid": opid or old.get("ebsco_opid", ""),
                            "resources": res or old.get("resources") or []})
            if opid and opid != litdl.EBSCO_OPID:
                litdl.set_env({"LITDL_EBSCO_OPID": opid})
            say(f"已记下：{pf}"
                + (f"；网址的特殊写法写进了 {hf}（{new}）" if new else "")
                + ("；" + "、".join(f"{k} 保留已记下的 {h['webvpn_hosts'][k]}（这一页给的是 {v}，没有采用）"
                                    for k, v in kept.items()) if kept else "")
                + (f"；opid 写进了 .env（{opid}）" if opid and opid != litdl.EBSCO_OPID else ""))
        else:
            say("没有保存。加 --save 记到 ~/.litdl/portal.json，并把特殊写法和 opid 写进配置。")
        if mine:
            page.close()
    return 0


# --------------------------------------------------------------------------- primitives
def cmd_tabs(a):
    """From the debugging port's tab list: attaches nothing, so it is safe while a verification page is up."""
    for t in bare_tabs():
        title = t.get("title") or ""
        say(f"{t['id'][:6]}  {title[:50]!r:54s} {(t.get('url') or '')[:110]}"
            + ("  [验证页]" if CHALLENGE_TITLE.search(title) else ""))
    return 0


def cmd_goto(a):
    with Session(exe=a.exe) as sess:
        page = sess.find(a.tab)
        try:
            resp = page.goto(a.url, wait_until="domcontentloaded", timeout=60000)
            code = resp.status if resp else "-"
        except Exception as e:
            code = str(e).splitlines()[0][:80]
        try:
            page.wait_for_timeout(int(a.wait * 1000))
            st = page_state(page)
        except Exception as e:                        # the page took the whole browser down with it (seen 2026-11-21)
            if cdp_version():
                raise
            say(f"专用 Chrome 在打开这一页时退出了（{code}）。再用它会自动重启，但 WebVPN 的登录没了，要用户重新登录。"
                f"这个地址不要再试：{a.url[:100]}")
            return 1
        say(json.dumps({"tab": sess.tab_id(page), "http": code, **st}, ensure_ascii=False))
    return 0


def cmd_eval(a):
    js = Path(a.js[1:]).read_text(encoding="utf-8") if a.js.startswith("@") else a.js
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        val = page.evaluate(js)
        out = json.dumps(val, ensure_ascii=False)
        say(out if len(out) <= a.max else out[:a.max] + f" …[+{len(out) - a.max}]")
    return 0


def cmd_text(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        t = re.sub(r"\n\s*\n+", "\n", page.inner_text("body"))
        say(t[:a.max])
    return 0


def cmd_click(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        loc = page.locator(a.selector).first if a.selector else page.get_by_text(a.text, exact=a.exact).first
        if a.download:
            watch = DownloadWatch(sess.ctx, page)
            try:
                loc.click(timeout=10000, no_wait_after=True)
                t0 = time.time()
                while not watch.items and time.time() - t0 < a.timeout:
                    page.wait_for_timeout(700)
                if not watch.items:
                    say("点了，但没有下载开始")
                    return 1
                res = save_download(watch.items[-1], a.download, a.timeout)
                say(json.dumps(res, ensure_ascii=False))
                return 0 if res["outcome"] == "ok" else 1
            finally:
                watch.close()
        loc.click(timeout=10000)
        page.wait_for_timeout(int(a.wait * 1000))
        say(json.dumps({"tab": sess.tab_id(page), **page_state(page)}, ensure_ascii=False))
    return 0


def cmd_fill(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        page.locator(a.selector).first.fill(a.value, timeout=10000)
        say("ok")
    return 0


def cmd_shot(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=a.out, full_page=a.full, timeout=30000)
        say(a.out)
    return 0


def cmd_widgets(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        say(json.dumps(captcha_ai._widget_hint(page), ensure_ascii=False))
    return 0


def cmd_clickat(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        page.bring_to_front()
        if not (a.os and captcha_ai._os_click(page, a.x, a.y, log=say)):
            page.mouse.move(a.x, a.y)
            page.wait_for_timeout(120)
            page.mouse.click(a.x, a.y)
        page.wait_for_timeout(int(a.wait * 1000))
        say(f"clicked ({a.x},{a.y})  " + json.dumps(page_state(page), ensure_ascii=False))
    return 0


def cmd_drag(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        page.bring_to_front()
        if not (a.os and captcha_ai._os_drag(page, a.x1, a.y1, a.x2, a.y2, duration=a.duration)):
            captcha_ai.drag(page, a.x1, a.y1, a.x2 - a.x1, a.y2 - a.y1, duration=a.duration)
        page.wait_for_timeout(int(a.wait * 1000))
        say(f"dragged ({a.x1},{a.y1}) -> ({a.x2},{a.y2})  " + json.dumps(page_state(page), ensure_ascii=False))
    return 0


def cmd_hoverat(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        page.bring_to_front()
        page.mouse.move(a.x, a.y)
        page.wait_for_timeout(int(a.wait * 1000))
        say(f"hover ({a.x},{a.y})  " + json.dumps(page_state(page), ensure_ascii=False))
    return 0


def cmd_front(a):
    with Session(autostart=False) as sess:
        page = sess.find(a.tab)
        page.bring_to_front()
        say(f"已放到前台：{sess.tab_id(page)}  {safe_title(page)[:50]!r}  {page.url[:100]}")
    return 0


def cmd_close(a):
    with Session(autostart=False) as sess:
        if len(sess.ctx.pages) - len(a.tab) < 1:
            sess.ctx.new_page()                       # closing the last tab would close the window and the session
        for key in a.tab:
            p = sess.find(key)
            say(f"关闭 {safe_title(p)[:40]!r} {p.url[:70]}")
            p.close()
    return 0


# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", help="browser executable (default: installed Chrome, then Edge)")
    ap.add_argument("--use-proxy", action="store_true",
                    help="start Chrome through the system proxy (Clash). Only when the user asks for it: the default "
                         "is a direct connection. Takes effect only when Chrome is (re)started")
    ap.add_argument("--no-proxy", action="store_true",
                    help="accepted for older command lines; a direct connection is the default now")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def common(p, vpn=True):
        if vpn:
            p.add_argument("--webvpn", default=litdl.WEBVPN_DEFAULT,
                           help="school WebVPN host (default: what `litdl.py config --webvpn` saved)")
        return p

    def captcha_args(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--auto-captcha", dest="auto_captcha", action="store_true", default=None,
                       help="try the vision model on click/slider widgets before asking the user "
                            "(default: on when a key is configured)")
        g.add_argument("--no-auto-captcha", dest="auto_captcha", action="store_false",
                       help="always stop and ask the user")
        p.add_argument("--captcha-attempts", type=int,
                       help="model attempts per verification (default 3, max 6)")

    p = sp.add_parser("open", help="start the dedicated Chrome")
    p.add_argument("--url", action="append", help="also open this page (repeatable)")
    p = common(sp.add_parser("status", help="Chrome / WebVPN session / tabs"))
    p.add_argument("--no-vpn", action="store_true", help="skip the portal request")
    p = common(sp.add_parser("login", help="open a login page for the user"))
    p.add_argument("site", choices=sorted(LOGIN_PAGES))
    p.add_argument("--wait", type=int, default=0, help="seconds to wait for the user to finish logging in")

    p = common(sp.add_parser("portal", help="after login: read the WebVPN portal page and list the databases it offers"))
    p.add_argument("--url", help="read this page instead of the portal home (the place the user said the list is)")
    p.add_argument("--tab", help="read a page the user has already opened in the dedicated Chrome")
    p.add_argument("--library", action="store_true",
                   help="read the school library's resource list (bought / trial / free) instead of the portal home. "
                        "Only for schools whose path is in scripts/schools.json; for others it prints what to ask the user")
    p.add_argument("--wait", type=float, default=4, help="seconds to let the page render")
    p.add_argument("--save", action="store_true",
                   help="record what was found in ~/.litdl/portal.json; write host quirks and the EBSCO id into the config")
    p.add_argument("--no-follow", action="store_true", help="do not open the EBSCO entry to read the customer id")

    p = common(sp.add_parser("fetch", help="download manifest rows one by one"))
    p.add_argument("manifest")
    p.add_argument("--stage", required=True, help="staging folder: <id>.pdf, results.jsonl, progress.json")
    p.add_argument("--via", default="auto", choices=["auto", *RECIPES],
                   help="auto: per row, the next database that can hold the paper and is not in its ledger yet "
                        "(school database for the publisher -> scihub -> public copy; see `litdl.py locate`)")
    p.add_argument("--ids", help="comma-separated row ids")
    p.add_argument("--bucket", help="only these buckets (comma-separated)")
    p.add_argument("--year-max", type=int)
    p.add_argument("--year-min", type=int)
    p.add_argument("--limit", type=int, default=25, help="papers per run (0 = no cap); one publisher should not get more than 25-30 in a round")
    p.add_argument("--opid", default=litdl.EBSCO_OPID,
                   help="EBSCO customer id in the WebVPN address (default: what `litdl.py config --ebsco-opid` saved)")
    p.add_argument("--human-wait", type=int, default=300, help="seconds to wait for the user at a verification page")
    p.add_argument("--timeout", type=int, help="seconds for one PDF (default per channel; EBSCO 480)")
    p.add_argument("--delay", type=float, help="base gap between papers (default per channel)")
    p.add_argument("--max-delay", type=float, default=300)
    p.add_argument("--rest-every", type=int, default=6)
    p.add_argument("--stop-after", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="list what would be requested, send nothing")
    p.add_argument("--detach", action="store_true", help="run as a process of its own (long rounds); output in <stage>/fetch.log")
    captcha_args(p)

    p = common(sp.add_parser("get", help="one URL -> <stage>/<id>.pdf"))
    p.add_argument("--url", required=True)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--stage", required=True)
    p.add_argument("--tab", help="reuse this tab (needed with --inpage: the tab must already be on the same site)")
    p.add_argument("--inpage", action="store_true", help="fetch() inside the tab instead of navigating")
    p.add_argument("--channel", help="ledger name written to results.jsonl (ebsco / publisher / scihub / public / ablesci). Default: scihub for a Sci-Hub address, publisher for an address behind the school WebVPN, public otherwise")
    p.add_argument("--human-wait", type=int, default=300)
    p.add_argument("--timeout", type=int)
    captcha_args(p)

    p = sp.add_parser("solve", help="one attempt at the verification on one tab, nothing attached: known checkbox / "
                                    "Sci-Hub's button with the system mouse, else the vision model")
    p.add_argument("--tab", required=True)
    p.add_argument("--attempts", type=int, help="override LITDL_CAPTCHA_ATTEMPTS for this run")
    p = sp.add_parser("look", help="what a tab shows, nothing attached: title, verification controls, text; "
                                   "--out saves a screenshot. Use this on verification pages, not shot / widgets")
    p.add_argument("--tab", required=True)
    p.add_argument("--out", help="also save a PNG screenshot of the viewport here (device pixels)")
    p.add_argument("--max", type=int, default=600, help="characters of page text to print")
    p = sp.add_parser("press", help="click (or drag / hover) one CSS-pixel point with the system mouse, nothing "
                                    "attached. Use this on verification pages, not clickat / drag")
    p.add_argument("--tab", required=True)
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.add_argument("--to-x", type=float, help="drag to this point instead of clicking")
    p.add_argument("--to-y", type=float)
    p.add_argument("--hover", action="store_true", help="only move the cursor there")
    p.add_argument("--duration", type=float, help="seconds for a drag")
    p.add_argument("--wait", type=float, default=3, help="seconds before reading the tab's title again")

    p = sp.add_parser("progress", help="progress of a fetch")
    p.add_argument("stage")
    p.add_argument("--last", type=int, default=8)
    p = sp.add_parser("wait", help="block until a fetch ends or needs the user (at most --timeout seconds)")
    p.add_argument("stage")
    p.add_argument("--timeout", type=int, default=540)
    p.add_argument("--ignore-need-user", action="store_true", help="keep waiting while a verification page is showing")
    p.add_argument("--last", type=int, default=8)
    p = sp.add_parser("pause", help="stop a running fetch after the current paper")
    p.add_argument("stage")
    p = sp.add_parser("quit", help="close the dedicated Chrome")
    p.add_argument("--stage")
    p.add_argument("--force", action="store_true")

    sp.add_parser("tabs", help="list tabs (from the debugging port's list: attaches nothing)")
    p = sp.add_parser("goto", help="navigate a tab, print its state")
    p.add_argument("url")
    p.add_argument("--tab", default="new")
    p.add_argument("--wait", type=float, default=4)
    p = sp.add_parser("eval", help="run JS in a tab, print the JSON result")
    p.add_argument("js", help="an arrow function or expression, or @file.js")
    p.add_argument("--tab", required=True)
    p.add_argument("--max", type=int, default=3000)
    p = sp.add_parser("text", help="visible text of a tab")
    p.add_argument("--tab", required=True)
    p.add_argument("--max", type=int, default=3000)
    p = sp.add_parser("click", help="click by selector or text; --download saves the file the click triggers")
    p.add_argument("--tab", required=True)
    p.add_argument("--selector")
    p.add_argument("--text")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--download", help="save the triggered download to this path")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--wait", type=float, default=2)
    p = sp.add_parser("fill", help="type into an input")
    p.add_argument("--tab", required=True)
    p.add_argument("--selector", required=True)
    p.add_argument("--value", required=True)
    p = sp.add_parser("shot", help="screenshot a tab to a PNG (attaches to the browser: on a verification page use look --out)")
    p.add_argument("--tab", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--full", action="store_true")
    p = sp.add_parser("widgets", help="list known captcha iframe boxes on a tab (attaches: on a verification page use look)")
    p.add_argument("--tab", required=True)
    p = sp.add_parser("clickat", help="click one CSS-pixel point (attaches: on a verification page use press)")
    p.add_argument("--tab", required=True)
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.add_argument("--wait", type=float, default=1)
    p.add_argument("--os", action="store_true", help="click with the Windows cursor instead of CDP")
    p = sp.add_parser("drag", help="press, move along an eased arc, release (attaches: on a verification page use press --to-x/--to-y)")
    p.add_argument("--tab", required=True)
    p.add_argument("--x1", type=float, required=True)
    p.add_argument("--y1", type=float, required=True)
    p.add_argument("--x2", type=float, required=True)
    p.add_argument("--y2", type=float, required=True)
    p.add_argument("--duration", type=float)
    p.add_argument("--wait", type=float, default=1)
    p.add_argument("--os", action="store_true", help="drag with the Windows cursor instead of CDP")
    p = sp.add_parser("hoverat", help="move the mouse to a point and hold it (attaches: on a verification page use press --hover)")
    p.add_argument("--tab", required=True)
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.add_argument("--wait", type=float, default=2)
    p = sp.add_parser("front", help="bring a tab to the front (for the user to log in or pass a verification)")
    p.add_argument("--tab", required=True)
    p = sp.add_parser("close", help="close tabs")
    p.add_argument("tab", nargs="+")

    a = ap.parse_args()
    if getattr(a, "webvpn", None) is not None:
        litdl.WEBVPN_DEFAULT = a.webvpn.strip()  # this command's --webvpn also decides which databases are tried
    global CHROME_DIRECT
    if a.use_proxy and a.no_proxy:
        ap.error("--use-proxy and --no-proxy contradict each other")
    if a.use_proxy:
        CHROME_DIRECT = False
    elif a.no_proxy:
        CHROME_DIRECT = True
    if a.cmd == "click" and not (a.selector or a.text):
        ap.error("click needs --selector or --text")
    if getattr(a, "auto_captcha", None) is not None:
        captcha_ai.set_enabled(a.auto_captcha)
    if getattr(a, "captcha_attempts", None):
        captcha_ai.set_attempts(a.captcha_attempts)
    fn = {"open": cmd_open, "status": cmd_status, "login": cmd_login, "portal": cmd_portal, "fetch": cmd_fetch, "get": cmd_get,
          "solve": cmd_solve, "progress": cmd_progress, "wait": cmd_wait, "pause": cmd_pause, "quit": cmd_quit,
          "tabs": cmd_tabs, "goto": cmd_goto, "eval": cmd_eval, "text": cmd_text, "click": cmd_click,
          "fill": cmd_fill, "shot": cmd_shot, "close": cmd_close,
          "widgets": cmd_widgets, "clickat": cmd_clickat, "drag": cmd_drag, "hoverat": cmd_hoverat,
          "front": cmd_front, "look": cmd_look, "press": cmd_press}[a.cmd]
    return fn(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
