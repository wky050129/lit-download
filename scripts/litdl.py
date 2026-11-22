#!/usr/bin/env python3
"""litdl - paper download helper.

Subcommands:
  config    show or fill in the user's own school (WebVPN host, EBSCO customer id). First thing in a new place:
            nothing about any school is built in, and plan / fetch / login stop until the WebVPN host is given
  plan      list of DOIs / arXiv IDs -> manifest.json + .xlsx (metadata, bucket, route, target file name, WebVPN links)
  alts      look up public copies (repository / SSRN / arXiv) of the rows still missing, via OpenAlex
  arxiv     download arXiv rows (stdlib only, so it also runs inside WSL; supports --proxy and --probe)
  vpnfetch  fallback: IEEE / Springer rows through the school WebVPN with curl (cookies from the litbrowser Chrome or scansci)
  ingest    match PDFs in source folders to manifest rows, verify them, name them, file them, update the manifest;
            also merges the results.jsonl a litbrowser fetch leaves in a source folder into each row's `tried` ledger
  merge     fold <stage>/results.jsonl into the `tried` ledger when a round brought no file to ingest
  mark      edit status / route / note / version / tried of some rows (the only way to hand-edit the manifest: it refreshes the xlsx)
  rename    after the user agreed a new abbreviation in journal_abbr.json: give the rows already filed that abbreviation
            (only the "(YY ABBR)" part of the file name changes; --dry-run first)
  missing   status summary + hand-over table of everything not yet downloaded; --exhausted lists rows with every
            database that can hold them tried
  locate    per missing paper: which databases can hold it (school holdings, Sci-Hub, public copies), in the order
            to try them, and which one is next. Sends no request

The browser channel (EBSCO, Wiley, Springer, IEEE, Elsevier, public copies) lives in litbrowser.py.

On this Windows machine run it with `python -P` (openpyxl / PyMuPDF live in the user site-packages; -I would hide them).
Only `ingest` needs PyMuPDF and only the .xlsx export needs openpyxl; both are imported lazily.
"""
import argparse
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
UA = "litdl/1.0 (literature download helper)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
DEFAULT_COOKIES = "~/.scansci-pdf/cache/instsci-cookies.json"
MAX_PATH = 259


def load_env():
    """KEY=VALUE lines from ~/.litdl/.env (or $LITDL_HOME/.env). Real environment variables win."""
    path = Path(os.environ.get("LITDL_HOME") or (Path.home() / ".litdl")) / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):        # an empty variable in the environment does not hide the file
            os.environ[k] = v


load_env()
# School settings. Nothing is built in: the WebVPN host and the EBSCO customer id are the user's own school's,
# filled in once with `litdl.py config` (kept in ~/.litdl/.env, see references/setup.md).
LITDL_HOME = Path(os.environ.get("LITDL_HOME") or (Path.home() / ".litdl"))
WEBVPN_DEFAULT = (os.environ.get("LITDL_WEBVPN") or "").strip()     # a command's --webvpn replaces it for that command
CONFIGURED_WEBVPN = WEBVPN_DEFAULT                                    # what the user's answer, saved by `config`, says
EBSCO_OPID = (os.environ.get("LITDL_EBSCO_OPID") or "").strip()
NO_VPN = {"none", "no", "-", "无"}                    # `config --webvpn none`: this user has no school WebVPN


def need_webvpn(value):
    """The school's WebVPN host is asked for once, before anything else (user, 2026-11-21); nothing is assumed.
    -> the host, or '' when the user said there is none. Exits with what to do when it was never filled in."""
    v = (value or "").strip()
    # 2026-11-21 re-test: an agent saw "not filled in", added --webvpn none to every command on its own and so skipped
    # every school database. "none" on one command line counts only when the user's answer is already on record.
    if not v or (v.lower() in NO_VPN and not CONFIGURED_WEBVPN):
        print("NEED_USER  还没填学校的 WebVPN 地址。这一步不能跳过，也不要自己在命令上加 --webvpn none 绕过去：问用户学校 "
              "WebVPN 门户的网址（浏览器里打开门户时地址栏里的主机名，形如 webvpn.xxx.edu.cn），然后跑 "
              "litdl.py config --webvpn <域名>。用户说学校没有 WebVPN，才跑 litdl.py config --webvpn none。", flush=True)
        sys.exit(2)
    return "" if v.lower() in NO_VPN else v

# DOI prefix -> bucket
PREFIX = {
    "10.1016": "elsevier", "10.1109": "ieee", "10.1007": "springer", "10.1186": "springer",
    "10.1287": "informs", "10.1002": "wiley", "10.1111": "wiley", "10.1029": "wiley", "10.2134": "wiley",
    "10.1177": "sage", "10.1080": "tandf", "10.1108": "emerald", "10.1145": "acm",
    "10.1038": "nature", "10.1126": "science", "10.1073": "pnas", "10.1093": "oup", "10.1017": "cambridge",
    "10.2139": "ssrn", "10.3390": "mdpi", "10.5194": "copernicus", "10.3389": "frontiers",
    "10.1371": "plos", "10.1101": "biorxiv", "10.48550": "arxiv",
}
# bucket -> suggested route (shown in manifest / hand-over table)
ROUTE = {
    "arxiv": "arXiv 直下（litdl arxiv）",
    "oa": "OA 直链 / scansci 小批量 / litbrowser --via public",
    "ieee": "浏览器·WebVPN（litbrowser fetch）",
    "springer": "浏览器·WebVPN（litbrowser fetch）",
    "elsevier": "浏览器·WebVPN·ScienceDirect（litbrowser fetch，逐篇）",
    "informs": "浏览器·WebVPN·EBSCO（litbrowser fetch）",
    "wiley": "浏览器·WebVPN·Wiley（litbrowser fetch；没订会跳 /doi/abs）",
    "sage": "先 EBSCO 再公开版本（litbrowser --via ebsco / public）",
    "tandf": "先 EBSCO 再公开版本（litbrowser --via ebsco / public）",
    "emerald": "先 EBSCO 再公开版本（litbrowser --via ebsco / public）",
    "acm": "公开版本（litbrowser --via public，可能要用户点验证）",
    "ssrn": "SSRN 页面（litbrowser --via public，可能要用户点验证）",
    "cnki": "知网（用户手动）",
    "unknown": "Crossref、OpenAlex 都查不到：先核对 DOI 有没有写错；DOI 没错、确是中文文章再走知网",
    "other": "先 EBSCO 再公开版本（litbrowser --via ebsco / public）",
}
# the names a row's `tried` ledger may use. Which of them one row needs before it may go to 科研通 is decided per
# row by required(): the databases that can hold this paper, not all four for everybody
CHANNELS = ("ebsco", "publisher", "scihub", "public")
# litbrowser recipe -> ledger name
LEDGER = {"ebsco": "ebsco", "wiley": "publisher", "direct": "publisher", "elsevier": "publisher", "vpnpub": "publisher",
          "tandf": "publisher", "pubdoi": "publisher", "scihub": "scihub", "public": "public"}
SOURCE_CN = {"ebsco": "EBSCO（WebVPN）", "wiley": "Wiley（WebVPN）", "direct": "出版商（WebVPN）",
             "elsevier": "ScienceDirect（WebVPN）", "vpnpub": "出版商的 WebVPN 入口",
             "tandf": "T&F 官网（校园 IP）", "pubdoi": "出版商官网（校园 IP）",
             "scihub": "Sci-Hub", "public": "公开版本"}
# Which of the school's databases holds a publisher's journals: bucket (or exact journal name) -> recipes, in the
# order to try them. This built-in table is only a starting point (a publisher with its own platform -> that
# platform, business journals also -> EBSCO). scripts/holdings.json overrides it, and the user's own
# ~/.litdl/holdings.json - what was actually measured at their school - overrides both (references/setup.md).
HOLDINGS = {
    "bucket": {
        "informs": ["ebsco"],
        "wiley": ["wiley", "ebsco"],
        "springer": ["direct"],
        "ieee": ["direct"],
        "elsevier": ["elsevier"],
        "tandf": ["tandf", "ebsco"],
        "emerald": ["pubdoi", "ebsco"],
        "sage": ["pubdoi", "ebsco"],
        "other": ["ebsco"],
    },
    "journal": {},
    "scihub": True,
    # original host -> the form it takes behind this school's WebVPN, for portals that do not follow the usual
    # <host with dashes>-443.<vpn> rule for some site, e.g. {"link.springer.com": "link-springer-com.{vpn}:443"}
    "webvpn_hosts": {},
}
HOLDINGS_FILES = (HERE / "holdings.json", LITDL_HOME / "holdings.json")     # later wins


def load_holdings():
    h = {"bucket": dict(HOLDINGS["bucket"]), "journal": dict(HOLDINGS["journal"]), "scihub": HOLDINGS["scihub"],
         "webvpn_hosts": dict(HOLDINGS["webvpn_hosts"]), "files": []}
    for f in HOLDINGS_FILES:
        try:
            j = json.loads(f.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        except ValueError as e:
            sys.exit(f"收录表 {f} 不是合法的 JSON：{e}")
        h["files"].append(str(f))
        for k in ("bucket", "journal", "webvpn_hosts"):
            h[k].update({name.lower() if k == "journal" else name: v for name, v in (j.get(k) or {}).items()
                         if isinstance(v, (list, str))})
        if "scihub" in j:
            h["scihub"] = bool(j["scihub"])
    return h


def holdings():
    global _HOLD
    if _HOLD is None:
        _HOLD = load_holdings()
    return _HOLD


_HOLD = None
# OA on paper, but these publishers block scripts (Cloudflare / TLS fingerprint) -> open in Chrome
SCRIPT_HOSTILE = {"wiley", "sage", "acm", "tandf", "emerald", "pnas", "elsevier", "informs", "springer", "ieee", "other"}
OA_FRIENDLY = {"nature", "copernicus", "frontiers", "plos", "mdpi", "biorxiv", "science", "pnas", "oup", "cambridge"}
STATUS_CN = {"todo": "待下载", "done": "已下载", "have": "库中已有", "manual": "需手动"}

CJK = re.compile(r"[一-鿿]")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)


# --------------------------------------------------------------------------- helpers
def direct_opener(proxy=None):
    """urllib would pick up the Windows system proxy (Clash) on its own. Nothing here goes through it unless a
    proxy is named explicitly (user, 2026-11-21: downloads do not use the Clash proxy)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))


def http_json(url, timeout=30, tries=3, proxy=None):
    opener = direct_opener(proxy)
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with opener.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:                            # throttled: wait as long as the server asks, else back off
                ra = (e.headers.get("Retry-After") or "") if e.headers else ""
                time.sleep(min(120.0, float(ra)) if ra.isdigit() else 10 * (k + 1) + random.uniform(0, 3))
            else:
                time.sleep(2 + random.uniform(0, 2))
        except Exception:
            time.sleep(2 + random.uniform(0, 2))
    return None


THROTTLE_CODES = {"403", "429", "503"}                   # HTTP codes that usually mean "slow down", not "missing"


class Pacer:
    """Spacing between requests to one host, so a batch never turns into a rapid burst.

    - every wait is the current gap with random jitter, never a fixed beat;
    - after every `rest_every` items there is a longer rest;
    - a failure doubles the gap (a throttle code quadruples it, at least 60 s), up to `max_gap`;
    - three successes in a row shrink the gap by 30 %, never below the base;
    - `stop_after` consecutive failures trip the pacer: the caller stops instead of hammering a blocked host.
    """

    def __init__(self, base, max_gap=300.0, rest_every=8, stop_after=3):
        self.base = max(float(base), 1.0)
        self.gap = self.base
        self.max_gap = max(float(max_gap), self.base)
        self.rest_every = int(rest_every or 0)
        self.stop_after = int(stop_after or 0)
        self.n = self.fails = self.streak = 0

    def record(self, ok, hard=False):
        self.n += 1
        if ok:
            self.fails = 0
            self.streak += 1
            if self.streak >= 3 and self.gap > self.base:
                self.gap = max(self.base, self.gap * 0.7)
                self.streak = 0
        else:
            self.fails += 2 if hard else 1
            self.streak = 0
            self.gap = min(self.max_gap, max(self.gap * (4 if hard else 2), 60.0 if hard else 0.0))

    @property
    def tripped(self):
        return bool(self.stop_after) and self.fails >= self.stop_after

    def wait(self, stop=None):
        """Sleep the current gap. `stop` (a callable) is polled every 2 s so a pause request ends the wait at once."""
        t = self.gap * random.uniform(0.7, 1.6)
        rest = bool(self.rest_every) and self.n > 0 and self.n % self.rest_every == 0
        if rest:
            t += max(self.base, 3.0) * random.uniform(5, 10)
        print(f"       wait {t:.0f}s (gap {self.gap:.0f}s{', batch rest' if rest else ''})", flush=True)
        end = time.time() + t
        while time.time() < end:
            if stop and stop():
                return False
            time.sleep(min(2.0, max(0.0, end - time.time())))
        return True


def clean_doi(s):
    s = s.strip().rstrip(".,;")
    # a closing bracket with no opening one belongs to the sentence, not the DOI: Emerald prints
    # "[DOI 10.1108/SCM-10-2021-0478]" on page 1, and the trailing "]" kept ingest from matching the row
    while s and s[-1] in ")]" and s.count("(["[")]".index(s[-1])]) < s.count(s[-1]):
        s = s[:-1].rstrip(".,;")
    return s.lower()


def parse_id(s):
    """Return ('doi', doi) / ('arxiv', id) / None for one free-text identifier."""
    s = str(s or "").strip()
    if not s or s.startswith("#"):
        return None
    m = re.search(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", s, re.I) or \
        re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", s, re.I) or \
        re.fullmatch(r"(?:arxiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?", s, re.I)
    if m:
        return ("arxiv", m.group(1))
    m = DOI_RE.search(s)
    if m:
        return ("doi", clean_doi(m.group(0)))
    return None


def norm(s):
    s = str(s or "").replace("&", " and ")
    if not CJK.search(s):
        s = unicodedata.normalize("NFKD", s)
        # dashes, curly quotes and other punctuation become a space *before* the ASCII fold; otherwise
        # "learning–enabled" (en dash) collapses into "learningenabled" and stops matching "learning enabled"
        s = "".join(" " if unicodedata.category(c)[0] in "PSZ" else c for c in s)
        s = s.encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^0-9a-z一-鿿]+", " ", s)
    s = re.sub(r"^the ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def load_abbr():
    with open(HERE / "journal_abbr.json", encoding="utf-8") as f:
        raw = json.load(f)
    return {norm(k): v for k, v in raw.items() if not k.startswith("_")}


STOP = {"of", "and", "the", "on", "for", "in", "a", "an", "to", "part", "b"}


def abbr_clash(abbr, journal, abbr_map):
    """The (normalised) name of another journal that already uses `abbr` in the table, or ''.
    2026-11-22: Journal of Cleaner Production was guessed JCP, which the table gives Journal of Consumer Psychology."""
    me = norm(journal or "")
    return next((name for name, ab in abbr_map.items() if ab == abbr and name != me), "")


def guess_abbr(container, abbr_map):
    """-> (abbr, guessed?)"""
    if not container:
        return ("NA", True)
    if CJK.search(container):
        return (container.strip(), False)          # Chinese journals keep their full name
    n = norm(container)
    if n in abbr_map:
        return (abbr_map[n], False)
    m = re.search(r"\(([A-Z][A-Za-z]*[A-Z])\)", container)  # conference acronym, e.g. (CVPR)
    if m:
        return (m.group(1), False)
    words = [w for w in re.split(r"[\s\-–:,/]+", container) if w and w.lower() not in STOP and not w.isdigit()]
    return ("".join(w[0].upper() for w in words)[:8] or "NA", True)


def clean_title(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*[:?：？]\s+(?=\S)", "-", t)   # subtitle colon / mid-title ? -> '-'
    t = re.sub(r"[?:：？]+$", "", t)                # trailing ? dropped
    t = re.sub(r"\s*[:：]\s*", "-", t)
    t = re.sub(r"\s*[‐‑‒–—―]\s*", "-", t)   # hyphens / en / em dashes -> '-'
    t = t.replace("/", "-").replace("\\", "-")
    t = re.sub(r'[*"<>|?\x00-\x1f]', "", t)        # a ? left inside, e.g. "(and why?)", is not allowed by Windows
    return t.strip(" .")


BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name):
    """A file name Windows accepts: no backslash, slash, colon, * ? " < > | or control character anywhere in it."""
    stem, dot, ext = name.rpartition(".") if name.lower().endswith(".pdf") else (name, "", "")
    return BAD_CHARS.sub("", stem).strip(" .") + dot + ext


def target_name(row):
    yy = str(row.get("year_print") or row.get("year_online") or row.get("year") or "")[-2:] or "XX"
    title = row["title"] + ((": " + row["subtitle"]) if row.get("subtitle") else "")
    surname = BAD_CHARS.sub("", row.get("surname") or "") or "Anon"
    abbr = BAD_CHARS.sub("", row.get("abbr") or "") or "NA"
    return safe_filename(f"{surname}({yy} {abbr}){clean_title(title)}.pdf")


def ypart(m, key):
    try:
        return m[key]["date-parts"][0][0]
    except Exception:
        return None


def load(p):
    return json.load(open(p, encoding="utf-8"))


def save(p, rows):
    p = Path(p)
    tmp = p.with_suffix(".tmp")
    json.dump(rows, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def pdf_complete(path):
    """A cut-off download keeps the %PDF head but loses the %%EOF marker at the end."""
    p = Path(path)
    with p.open("rb") as f:
        if f.read(5) != b"%PDF-":
            return False
        f.seek(max(0, p.stat().st_size - 4096))
        return b"%%EOF" in f.read()


def add_tried(row, channel, text):
    row.setdefault("tried", {})[channel] = text


def tried_text(row):
    return "；".join(f"{k}：{v}" for k, v in (row.get("tried") or {}).items())


VPN_RECIPES = {"ebsco", "wiley", "direct", "elsevier", "vpnpub"}     # these go through the school WebVPN


def sources(row):
    """The databases that can hold this paper, in the order to try them (litbrowser recipe names). Decided per
    paper before anything is requested (user, 2026-11-21): an open-access copy first, then the school database
    that carries this publisher's journals, then Sci-Hub, then public copies (repository, SSRN, arXiv).
    arXiv rows go through `litdl arxiv`; CNKI and rows without metadata have no source here."""
    _HOLD = holdings()
    no_vpn = WEBVPN_DEFAULT.lower() in NO_VPN        # the user has no school WebVPN: nothing goes through one
    b = row.get("bucket")
    if b in ("arxiv", "cnki", "unknown") or not (row.get("doi") or row.get("title")):
        return []
    src = []
    if b in OA_FRIENDLY and (row.get("is_oa") or row.get("oa_pdf")):
        src.append("public")                         # an open-access publisher: its own page is the formal version
    school = _HOLD["journal"].get((row.get("journal") or "").lower())
    if school is None:
        school = _HOLD["bucket"].get(b, [])
    if WEBVPN_DEFAULT and not no_vpn:              # made now: a row planned before the school was known has none stored
        page, pdf = webvpn_links(row, WEBVPN_DEFAULT) if row.get("doi") else ("", "")
    else:
        page, pdf = row.get("webvpn_page"), row.get("webvpn_pdf")
    for r in school:
        usable = {"wiley": b == "wiley" and page,
                  "elsevier": b == "elsevier" and page and row.get("pii"),
                  "direct": pdf, "tandf": row.get("doi"), "pubdoi": row.get("doi"),
                  "vpnpub": page and b not in ("wiley", "elsevier", "springer", "ieee", "informs"),
                  "ebsco": row.get("title")}.get(r, True)
        if usable and r in LEDGER and r not in src and not (no_vpn and r in VPN_RECIPES):
            src.append(r)
    if _HOLD["scihub"] and row.get("doi"):
        src.append("scihub")
    if "public" not in src:
        src.append("public")
    return src


def required(row):
    """Ledger channels this row needs an entry for before it may go to 科研通: one per database in sources()."""
    return list(dict.fromkeys(LEDGER[s] for s in sources(row)))


def next_source(row, skip=()):
    """The first source whose channel has no conclusive entry in the ledger yet and that is not held up
    (see held_up), or None."""
    tried = row.get("tried") or {}
    return next((s for s in sources(row) if s not in skip and LEDGER[s] not in tried and not held_up(row, s)), None)


def route_text(row):
    """'EBSCO（WebVPN）✗ → Sci-Hub✓ → 公开版本' - the order for this row: ✓ this database gave the file, ✗ it was
    tried and has no copy (an answer, not a failure), ⏸ it stopped the row at a verification page or did not know the
    school (still open), nothing: not tried yet. (A bare ✓ for "has an answer" read as "has a copy": agents' reports.)"""
    src = sources(row)
    if not src:
        return ROUTE.get(row.get("bucket"), ROUTE["other"])
    tried = row.get("tried") or {}

    def mark(s):
        if LEDGER[s] in tried:
            return "✓" if str(tried[LEDGER[s]]).startswith("已下到") else "✗"
        return "⏸" if held_up(row, s) else ""
    return " → ".join(SOURCE_CN[s] + mark(s) for s in src)


OUTCOME_CN = {"ok": "已下到", "citation_only": "有题录无全文", "not_found": "搜不到", "no_access": "无权访问",
              "no_link": "页面上没有 PDF 链接", "need_user": "等用户点人机验证", "blocked": "被限制访问",
              "throttle": "被限流", "not_pdf": "返回的不是 PDF", "truncated": "下载被截断", "error": "出错", "login": "会话失效",
              "no_campus": "出版商没认出学校（不在校园网，不算试过）"}
# only these say something about the paper itself; the rest are transport trouble and do not count as "tried"
CONCLUSIVE = {"ok", "citation_only", "not_found", "no_access", "no_link"}


def merge_results(rows, src_dirs):
    """Fold <src>/results.jsonl (written by litbrowser fetch/get) into each row's `tried` ledger. Latest line wins."""
    by_id = {r["id"]: r for r in rows}
    n, lines = 0, []
    for s in src_dirs:
        f = Path(s).expanduser() / "results.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(line))
            except Exception:
                continue
    for x in sorted(lines, key=lambda x: x.get("at", "")):
        r = by_id.get(x.get("id"))
        if not r:
            continue
        # each line is applied once: `res_at` remembers, per channel, how far this row has been merged, so an old
        # "blocked" line does not come back after a later `mark` or a later result
        ch, at = x.get("channel", "public"), x.get("at", "")
        seen = r.setdefault("res_at", {})
        if at and at <= seen.get(ch, ""):
            continue
        if at:
            seen[ch] = at
        oc = x.get("outcome", "")
        text = f"{OUTCOME_CN.get(oc, oc)}（{x.get('at', '')[:10]}）" + (f" {x['detail']}" if x.get("detail") and oc != "ok" else "")
        # what is left unfinished is kept per channel (`stuck`): an answer from EBSCO must not wipe out the note that
        # the publisher's page stopped at a verification (2026-11-21: that note was lost and the row went back to
        # the same verification page the next round)
        stuck = r.setdefault("stuck", {})
        if oc in CONCLUSIVE:
            add_tried(r, ch, text[:160])
            stuck.pop(ch, None)
        elif r.get("status") not in ("done", "have"):
            stuck[ch] = text[:160]
        show_stuck(r)
        n += 1
    return n


def show_stuck(r):
    """`pending` is what the hand-over table prints: every channel of this row that still has unfinished business.
    A row whose file is in has none (an old "waiting for the user" line must not come back)."""
    if r.get("status") in ("done", "have"):
        r.pop("stuck", None)
    st = r.get("stuck") or {}
    if st:
        r["pending"] = "；".join(f"{k}：{v}" for k, v in st.items())[:400]
    else:
        r.pop("stuck", None)
        r.pop("pending", None)


CAMPUS_RECIPES = {"tandf", "pubdoi"}                 # open the publisher's own site; need the campus IP


def held_up(row, recipe):
    """True when this database last stopped the row at a verification page nobody passed, or (campus-IP recipes)
    did not know the school. That is about where the user is and will be the same next round, so the row goes on
    to the next database instead of knocking on the same door every round. The channel stays open in the ledger:
    the row is not "exhausted" until it has a real answer, and `fetch --via <recipe> --ids` goes back to it once
    the user has passed the verification or is on campus. Throttling, a lost session, a cut-off file: retried."""
    ch = LEDGER[recipe]
    note = (row.get("stuck") or {}).get(ch, "")
    if not note and (row.get("pending") or "").split("：")[0] == ch:      # manifests written before `stuck` existed
        note = row["pending"]
    return "人机验证" in note or (recipe in CAMPUS_RECIPES and "没认出学校" in note)


# --------------------------------------------------------------------------- WebVPN links (host-rewriting portals)
def vpn_host(host, vpn):
    """www.example.com -> https://www-example-com-443.<vpn>, the usual rule of host-rewriting WebVPN portals.
    A school whose portal maps some site differently says so under "webvpn_hosts" in its holdings.json."""
    special = holdings()["webvpn_hosts"].get(host)
    if special:
        return "https://" + special.replace("{vpn}", vpn)
    return f"https://{host.replace('.', '-')}-443.{vpn}"


def webvpn_links(row, vpn):
    """-> (page_url, direct_pdf_url) for the school WebVPN; '' when unknown."""
    if not vpn or not row.get("doi"):
        return ("", "")
    d, b = row["doi"], row["bucket"]
    if b == "elsevier" and row.get("pii"):
        return (f"{vpn_host('www.sciencedirect.com', vpn)}/science/article/pii/{row['pii']}", "")
    if b == "ieee" and row.get("arnumber"):
        base = vpn_host("ieeexplore.ieee.org", vpn)
        return (f"{base}/document/{row['arnumber']}", f"{base}/stampPDF/getPDF.jsp?tp=&arnumber={row['arnumber']}")
    if b == "springer":
        base = vpn_host("link.springer.com", vpn)     # some portals use another form for Springer: "webvpn_hosts"
        return (f"{base}/article/{d}", f"{base}/content/pdf/{d}.pdf")
    if b == "wiley":
        return (f"{vpn_host('onlinelibrary.wiley.com', vpn)}/doi/{d}", "")
    if b == "informs":
        return (f"{vpn_host('research.ebsco.com', vpn)}/c/{EBSCO_OPID or '<opid>'}/search", "")   # INFORMS via EBSCO
    hosts = {"sage": "journals.sagepub.com", "acm": "dl.acm.org", "tandf": "www.tandfonline.com",
             "emerald": "www.emerald.com"}
    if b in hosts:
        return (f"{vpn_host(hosts[b], vpn)}/doi/{d}", "")
    return ("", "")


# --------------------------------------------------------------------------- plan
def read_ids(path, col=None, subdir_col=None):
    """-> list of (kind, id, subdir)"""
    p = Path(path)
    out = []
    if p.suffix.lower() in (".xlsx", ".xlsm", ".csv"):
        if p.suffix.lower() == ".csv":
            import csv
            data = list(csv.reader(open(p, encoding="utf-8-sig")))
        else:
            from openpyxl import load_workbook
            ws = load_workbook(p, read_only=True, data_only=True).active
            data = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
        head = [h.strip().lower() for h in data[0]]
        ci = head.index(col.lower()) if col else next((i for i, h in enumerate(head) if "doi" in h), None)
        si = head.index(subdir_col.lower()) if subdir_col else None
        if ci is None:
            sys.exit("no DOI column found; pass --col")
        for r in data[1:]:
            pid = parse_id(r[ci] if ci < len(r) else "")
            if pid:
                out.append((*pid, (r[si] if si is not None and si < len(r) else "") or ""))
    else:
        for line in open(p, encoding="utf-8-sig"):
            parts = line.rstrip("\n").split("\t")
            pid = parse_id(parts[0])
            if pid:
                out.append((*pid, parts[1].strip() if len(parts) > 1 else ""))
    seen, uniq = set(), []
    for k, i, s in out:
        if (k, i) not in seen:
            seen.add((k, i))
            uniq.append((k, i, s))
    return uniq


def openalex_batch(dois):
    res = {}
    for i in range(0, len(dois), 40):
        chunk = [d for d in dois[i:i + 40] if "," not in d and "|" not in d]
        if not chunk:
            continue
        url = "https://api.openalex.org/works?per-page=50&filter=doi:" + "|".join(urllib.parse.quote(d, safe="/()") for d in chunk)
        j = http_json(url) or {}
        for w in j.get("results", []):
            if w.get("doi"):
                res[w["doi"].lower().replace("https://doi.org/", "")] = w
    for d in dois:                                   # fill gaps one by one, spaced out
        if d not in res:
            time.sleep(random.uniform(0.4, 1.0))
            w = http_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(d, safe="/()"))
            if w:
                res[d] = w
    return res


def oa_info(w):
    if not w:
        return (False, "")
    oa = (w.get("open_access") or {})
    pdf = (w.get("best_oa_location") or {}).get("pdf_url") or ""
    if not pdf:
        for loc in w.get("locations") or []:
            if loc.get("pdf_url"):
                pdf = loc["pdf_url"]
                break
    return (bool(oa.get("is_oa")), pdf or "")


def from_crossref(doi, m, abbr_map):
    row = {"doi": doi}
    row["title"] = html.unescape(re.sub(r"<[^>]+>", "", (m.get("title") or [""])[0])).strip()
    sub = (m.get("subtitle") or [""])
    sub = html.unescape(re.sub(r"<[^>]+>", "", sub[0])).strip() if sub else ""
    row["subtitle"] = sub if sub and norm(sub) not in norm(row["title"]) else ""
    auths = m.get("author") or []
    first = next((a for a in auths if a.get("sequence") == "first"), auths[0] if auths else {})
    fam, giv = first.get("family") or first.get("name") or "", first.get("given") or ""
    if CJK.search(row["title"]) and CJK.search(fam + giv):
        row["surname"] = (fam + giv).replace(" ", "")
    else:
        row["surname"] = fam.split()[-1] if fam else ""
    row["authors"] = "; ".join(f"{a.get('given', '')} {a.get('family', a.get('name', ''))}".strip() for a in auths[:6])
    row["year_print"] = ypart(m, "published-print") or ypart(m.get("journal-issue") or {}, "published-print")
    row["year_online"] = ypart(m, "published-online") or ypart(m, "issued")
    cont = (m.get("container-title") or [""])
    row["journal"] = html.unescape(cont[0]).strip() if cont else ""
    if m.get("type") == "posted-content" and not row["journal"]:
        row["journal"] = (m.get("institution") or [{}])[0].get("name", "") or "preprint"
    row["abbr"], row["abbr_guess"] = guess_abbr(row["journal"], abbr_map)
    row["pii"] = next((a for a in m.get("alternative-id") or [] if re.fullmatch(r"S\d{4}[\dX]{8,}", a.replace("-", "").replace("(", "").replace(")", ""))), "")
    res = ((m.get("resource") or {}).get("primary") or {}).get("URL", "")
    row["landing"] = res
    am = re.search(r"/document/(\d+)", res)
    row["arnumber"] = am.group(1) if am else ""
    return row


def _caps(s):
    return sum(1 for w in s.split() if w[:1].isupper())


def prefer_published_casing(row, w):
    """Crossref sometimes stores titles in sentence case; OpenAlex often keeps the published Title Case."""
    if not w:
        return
    ot = html.unescape(re.sub(r"<[^>]+>", "", w.get("title") or "")).strip()
    full = row["title"] + ((": " + row["subtitle"]) if row.get("subtitle") else "")
    if ot and norm(ot) == norm(full) and _caps(ot) > _caps(full):
        row["title"], row["subtitle"] = ot, ""


def from_openalex(w, abbr_map, arxiv_id=None):
    row = {"doi": "" if arxiv_id else (w.get("doi") or "").lower().replace("https://doi.org/", "")}
    row["title"] = html.unescape(w.get("title") or w.get("display_name") or "").strip()
    row["subtitle"] = ""
    au = (w.get("authorships") or [{}])
    name = ((au[0].get("author") or {}).get("display_name") or "") if au else ""
    row["surname"] = name.replace(" ", "") if CJK.search(name) else (name.split()[-1] if name else "")
    row["authors"] = "; ".join(((a.get("author") or {}).get("display_name") or "") for a in au[:6])
    row["year_print"] = None
    row["year_online"] = w.get("publication_year")
    src = ((w.get("primary_location") or {}).get("source") or {})
    row["journal"] = "arXiv" if arxiv_id else (src.get("display_name") or "")
    row["abbr"], row["abbr_guess"] = ("arXiv", False) if arxiv_id else guess_abbr(row["journal"], abbr_map)
    row["pii"] = row["arnumber"] = row["landing"] = ""
    row["author_source"] = "openalex"            # OpenAlex first authors have been wrong before -> verify from PDF
    return row


def scan_have(dirs):
    have = {}
    for d in dirs or []:
        for p in Path(d).rglob("*.pdf"):
            stem = p.stem
            t = stem.split(")", 1)[1] if ")" in stem[:60] else stem
            have.setdefault(norm(t), str(p))
    return have


def cmd_plan(a):
    a.webvpn = need_webvpn(a.webvpn)                 # asked for once, before the first manifest
    ids = read_ids(a.input, a.col, a.subdir_col)
    abbr_map = load_abbr()
    dois = [i for k, i, _ in ids if k == "doi"]
    print(f"{len(ids)} identifiers ({len(dois)} DOI, {len(ids) - len(dois)} arXiv); fetching metadata ...", flush=True)
    with ThreadPoolExecutor(2) as ex:                # 2 workers: metadata lookups are requests too, keep them gentle
        cr = dict(zip(dois, ex.map(lambda d: (http_json("https://api.crossref.org/works/" + urllib.parse.quote(d, safe="/()")) or {}).get("message"), dois)))
    oa = openalex_batch(dois)
    have = scan_have(a.have)
    rows = []
    for n, (kind, ident, sub) in enumerate(ids, 1):
        if kind == "arxiv":
            w = http_json("https://api.openalex.org/works/doi:10.48550/arxiv." + ident)
            row = from_openalex(w, abbr_map, ident) if w else {"title": "", "subtitle": "", "surname": "", "journal": "arXiv", "abbr": "arXiv", "abbr_guess": False}
            row.update(arxiv=ident, bucket="arxiv", is_oa=True, oa_pdf=f"https://arxiv.org/pdf/{ident}")
        else:
            m = cr.get(ident)
            if m:
                row = from_crossref(ident, m, abbr_map)
                prefer_published_casing(row, oa.get(ident))
            elif oa.get(ident):
                row = from_openalex(oa[ident], abbr_map)
            else:
                row = {"doi": ident, "title": "", "subtitle": "", "surname": "", "journal": "", "abbr": "NA", "abbr_guess": False, "note": "Crossref/OpenAlex 都查不到"}
            row["arxiv"] = ""
            if not row.get("title"):
                row["bucket"] = "unknown"
            elif CJK.search(row["title"]):
                row["bucket"] = "cnki"
            else:
                row["bucket"] = PREFIX.get(ident.split("/")[0], "other")
            row["is_oa"], row["oa_pdf"] = oa_info(oa.get(ident))
        row["id"] = n
        row["subdir"] = sub
        b = row["bucket"]
        row["webvpn_page"], row["webvpn_pdf"] = webvpn_links(row, a.webvpn)
        if b != "arxiv" and row.get("oa_pdf") and b in OA_FRIENDLY:
            row["route"] = ROUTE["oa"]
        else:                                        # which databases can hold it, in the order to try them
            row["route"] = route_text(row) + ("（标为 OA，待验证）" if row.get("is_oa") and b in SCRIPT_HOSTILE else "")
        row["target"] = target_name(row) if row.get("title") else ""
        row.setdefault("note", "")
        row.update(status="todo", file="", version="", pages=None, sha256="", tried={})
        if row["target"] and norm(row["title"] + (" " + row["subtitle"] if row.get("subtitle") else "")) in have:
            row["status"], row["file"] = "have", have[norm(row["title"] + (" " + row["subtitle"] if row.get("subtitle") else ""))]
        elif row.get("title") and norm(clean_title(row["title"])) in have:
            row["status"], row["file"] = "have", have[norm(clean_title(row["title"]))]
        rows.append(row)
    names = [r["target"].lower() for r in rows if r["target"]]
    dup = {x for x in names if names.count(x) > 1}
    for r in rows:
        if r["target"].lower() in dup:
            r["note"] = (r["note"] + "; 目标文件名重复").strip("; ")
    save(a.out, rows)
    export_xlsx(a.out, rows)
    summary(rows)
    guessed = sorted({(r["journal"], r["abbr"]) for r in rows if r.get("abbr_guess")})
    if guessed:
        print("\n缩写是猜的（确认后可加进 journal_abbr.json）：")
        for j, ab in guessed:
            clash = abbr_clash(ab, j, abbr_map)
            print(f"  {ab:10s} <- {j}" + (f"   （表里 {ab} 已经是 {clash}，两本刊的文件名会撞，换一个再加）" if clash else ""))
    oa_authors = [r["id"] for r in rows if r.get("author_source") == "openalex"]
    if oa_authors:
        print(f"\n第一作者来自 OpenAlex（需从 PDF 核对）：{oa_authors}")
    print(f"\nmanifest -> {a.out}")


# --------------------------------------------------------------------------- xlsx export
def export_xlsx(manifest_path, rows):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        print("(openpyxl missing, xlsx skipped)")
        return
    cols = [("序号", "id"), ("桶", "bucket"), ("状态", "status"), ("版本", "version"), ("年份", "year_print"),
            ("期刊", "journal"), ("缩写", "abbr"), ("标题", "title"), ("第一作者", "surname"), ("DOI/arXiv", "doi"),
            ("OA PDF", "oa_pdf"), ("WebVPN 页面", "webvpn_page"), ("建议渠道", "route"), ("目标文件名", "target"),
            ("文件", "file"), ("备注", "note"), ("已试渠道", "tried"), ("未了结", "pending"), ("公开版本", "alt_copies")]
    fills = {"done": "C6EFCE", "have": "DDEBF7", "manual": "FFEB9C", "todo": "FFC7CE"}
    wb = Workbook()
    ws = wb.active
    ws.title = "下载清单"
    ws.append([c for c, _ in cols])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        vals = []
        for _, k in cols:
            v = r.get(k)
            if k == "status":
                v = STATUS_CN.get(v, v)
            if k == "year_print":
                v = r.get("year_print") or r.get("year_online")
            if k == "doi" and not v:
                v = "arXiv:" + r.get("arxiv", "")
            if k == "tried":
                v = tried_text(r)
            if k == "alt_copies":
                v = "\n".join(x if isinstance(x, str) else (x.get("pdf") or x.get("landing") or "") for x in (v or []))
            vals.append("" if v is None else v)
        ws.append(vals)
        i = ws.max_row
        ws.cell(i, 3).fill = PatternFill("solid", fgColor=fills.get(r["status"], "FFFFFF"))
        for k, url in ((10, f"https://doi.org/{r['doi']}" if r.get("doi") else ""), (11, r.get("oa_pdf")), (12, r.get("webvpn_page"))):
            if url:
                ws.cell(i, k).hyperlink = url
                ws.cell(i, k).font = Font(color="0563C1", underline="single")
    for col, w in zip("ABCDEFGHIJKLMNOPQRS", (5, 9, 8, 12, 6, 30, 8, 60, 12, 28, 30, 30, 26, 70, 40, 30, 40, 30, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    out = Path(manifest_path).with_suffix(".xlsx")
    try:
        wb.save(out)
    except PermissionError:
        print(f"(xlsx is open in Excel, not updated: {out})")


# --------------------------------------------------------------------------- vpnfetch
def cmd_vpnfetch(a):
    a.webvpn = need_webvpn(a.webvpn)
    if not a.webvpn:
        sys.exit("配置里写的是没有学校 WebVPN（none），这条通道用不了。")
    rows = load(a.manifest)
    want = set(int(x) for x in a.ids.split(",")) if a.ids else None
    todo = [r for r in rows if r.get("webvpn_pdf") and r["status"] == "todo" and (want is None or r["id"] in want)]
    if not todo:
        print("nothing to fetch (only rows with a direct WebVPN PDF link: ieee / springer)")
        return 0
    stage = Path(a.stage)
    stage.mkdir(parents=True, exist_ok=True)
    if a.dry_run:
        for r in todo:
            print(r["id"], r["bucket"], r["webvpn_pdf"])
        return 0
    if a.from_browser:                                    # one login for both channels: read the session from the litbrowser Chrome
        import litbrowser
        with litbrowser.Session(autostart=False) as sess:
            cs = [c for c in sess.ctx.cookies() if c.get("domain", "").lstrip(".").endswith(a.webvpn)]
    else:
        cs = json.load(open(Path(a.cookies).expanduser(), encoding="utf-8"))
        if isinstance(cs, dict):
            cs = cs.get("cookies", [])
    tmpd = tempfile.mkdtemp(prefix="litdl_")
    cf = os.path.join(tmpd, "c.txt")
    ok = 0
    try:
        with open(cf, "w", encoding="utf-8") as f:            # Netscape cookie file, deleted in finally
            f.write("# Netscape HTTP Cookie File\n")
            for c in cs:
                d = c["domain"]
                f.write("\t".join([d, "TRUE" if d.startswith(".") else "FALSE", c.get("path", "/"), "TRUE", "0", c["name"], c["value"]]) + "\n")
        r = subprocess.run(["curl", "-s", "--noproxy", "*", "-b", cf, "-A", BROWSER_UA, "-m", "30", f"https://{a.webvpn}/"], capture_output=True)
        logged = a.marker.encode() in r.stdout
        print("WebVPN session:", "OK" if logged else "NOT LOGGED IN (litbrowser login webvpn --wait 300, then --from-browser)", flush=True)
        if not logged:
            return 2
        pacer = Pacer(a.delay, a.max_delay, a.rest_every, a.stop_after)
        for i, row in enumerate(todo):
            out = stage / f"{row['id']}.pdf"
            if out.exists() and out.read_bytes()[:5] == b"%PDF-":      # staged by an earlier run: no new request
                ok += 1
                print(f"{row['id']:>4} {row['bucket']:9s} SKIP already staged", flush=True)
                continue
            r = subprocess.run(["curl", "-s", "--noproxy", "*", "-b", cf, "-A", BROWSER_UA, "-L", "-m", "90", "-o", str(out),
                                "-w", "%{http_code} %{content_type} %{size_download}", row["webvpn_pdf"]],
                               capture_output=True, text=True)
            good = out.exists() and pdf_complete(out)
            if not good and out.exists():
                out.unlink()
            ok += good
            print(f"{row['id']:>4} {row['bucket']:9s} {'OK  ' if good else 'FAIL'} {r.stdout.strip()}", flush=True)
            pacer.record(good, hard=(r.stdout.split() or [""])[0] in THROTTLE_CODES)
            if pacer.tripped:
                left = ",".join(str(x["id"]) for x in todo[i + 1:])
                print(f"连续失败，已停止（会话过期、被限流或学校没订都有可能），没有请求剩下的 {len(todo) - i - 1} 篇"
                      f"{'：--ids ' + left if left else ''}。\n至少等 15 分钟，先用 --ids <一个序号> 单篇试探，通了再继续。", flush=True)
                break
            if i < len(todo) - 1:
                pacer.wait()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    print(f"{ok}/{len(todo)} PDFs in {stage}  ->  next: litdl ingest --src {stage} --move")
    return 0


# --------------------------------------------------------------------------- arxiv (stdlib only)
def cmd_arxiv(a):
    handlers = [urllib.request.ProxyHandler({"http": a.proxy, "https": a.proxy})] if a.proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)

    def get(url, timeout=90):
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with opener.open(req, timeout=timeout) as r:
            return r.read()

    if a.probe:
        t = time.time()
        try:
            b = get("https://arxiv.org/pdf/2106.09685", 60)
            dt = time.time() - t
            print(f"proxy={a.proxy or 'none'}  {len(b) / 1024:.0f} KB in {dt:.1f}s  ({len(b) / 1024 / max(dt, 0.01):.0f} KB/s)  head={b[:5]}")
        except Exception as e:
            print(f"proxy={a.proxy or 'none'}  FAIL {e}")
        return 0
    rows = load(a.manifest)
    todo = [r for r in rows if r.get("arxiv") and r["status"] == "todo"]
    stage = Path(a.stage)
    stage.mkdir(parents=True, exist_ok=True)
    ok = 0
    pacer = Pacer(a.delay, a.max_delay, a.rest_every, a.stop_after)   # arXiv asks for a few seconds between requests
    for i, r in enumerate(todo):
        out = stage / f"{r['id']}.pdf"
        if out.exists() and out.read_bytes()[:5] == b"%PDF-":
            ok += 1
            continue
        good = hard = False
        for k in range(3):
            try:
                b = get(f"https://arxiv.org/pdf/{r['arxiv']}")
                if b[:5] == b"%PDF-":
                    out.write_bytes(b)
                    ok += 1
                    good = True
                    print(f"{r['id']:>4} OK   {r['arxiv']} {len(b) // 1024} KB", flush=True)
                else:
                    print(f"{r['id']:>4} not a PDF ({b[:20]!r})", flush=True)
                break                                             # a non-PDF answer is not retried on the spot
            except urllib.error.HTTPError as e:
                print(f"{r['id']:>4} try {k + 1}: {e}", flush=True)
                if str(e.code) in THROTTLE_CODES:                 # throttled: leave it to the pacer, no instant retry
                    hard = True
                    break
                if e.code == 404:
                    break
                time.sleep(15 * (k + 1) + random.uniform(0, 5))
            except Exception as e:
                print(f"{r['id']:>4} try {k + 1}: {e}", flush=True)
                time.sleep(15 * (k + 1) + random.uniform(0, 5))
        pacer.record(good, hard=hard)
        # one line in the stage's results.jsonl, like litbrowser fetch: ingest folds it into the ledger, so an arXiv
        # row shows where it came from (an agent's report, 2026-11-21: "arxiv wrote nothing; I had to mark it by hand")
        with (stage / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": r["id"], "recipe": "arxiv", "channel": "public",
                                "outcome": "ok" if good else ("throttle" if hard else "error"),
                                "detail": f"arXiv {r['arxiv']}", "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False) + "\n")
        if pacer.tripped:
            print(f"连续失败，已停止，没有请求剩下的 {len(todo) - i - 1} 篇。至少等 15 分钟，或换一条线路（--probe 重测）再继续；"
                  f"已落地的文件重跑时会跳过。", flush=True)
            break
        if i < len(todo) - 1:
            pacer.wait()
    print(f"{ok}/{len(todo)} arXiv PDFs in {stage}")


# --------------------------------------------------------------------------- ingest
VERSION_MARKS = [
    (r"just accepted", "Just Accepted"),
    (r"author accepted manuscript|accepted manuscript|accepted for publication|this is the author'?s version", "录用稿"),
    (r"\barxiv:\s*\d{4}\.\d{4,5}", "arXiv 预印本"),
    (r"biorxiv", "bioRxiv 预印本"),
    (r"electronic copy available at.{0,40}ssrn|ssrn\.com/abstract", "SSRN 工作论文"),
    # only in the header: a published paper's text cites "NBER Working Paper ..." often enough (review 2026-11-22)
    (r"\bworking paper\b", "工作论文", 1500),
    # "deposited in" alone is not a mark: a Nature paper says its structures were "deposited in the PDB" (2026-11-21)
    (r"research repository|institutional repository|repository by the author|deposited in [^.]{0,40}(repository|archive)"
     r"|post-?print|author'?s? (final|accepted) version|faculty of [a-z ,]+\s+\d{4}-\d{2}-\d{2}", "知识库版本（多为录用稿，看首页）"),
]


def other_dois(text, want):
    """DOIs printed in the opening part of page 1 (the article's own DOI sits there: journal header, footnote, the
    "downloaded from" banner) that are not `want`. References further down are not looked at."""
    head = (text or "")[:1500]
    want = (want or "").lower()
    seen = []
    for x in DOI_RE.findall(head):
        d = clean_doi(x)
        if d == want or d.startswith(want + "/") or d in seen or d.startswith(OTHER_VERSION_DOI):
            continue
        if want.startswith(d) or len(d.split("/", 1)[-1]) < 6:
            continue                             # the row's own DOI cut by a line break ("10.1287/mksc" + newline)
        seen.append(d)
    return seen


# DOIs of another version of the same work (SSRN, arXiv, bioRxiv/medRxiv, Research Square, Zenodo, figshare): a copy
# that prints one of these is the right paper in another version, which ingest labels separately
OTHER_VERSION_DOI = ("10.2139/", "10.48550/", "10.1101/", "10.21203/", "10.5281/", "10.6084/")


def version_of(text, row=None):
    """What the first pages say about which version this is; '' when nothing marks it as other than the published one.
    A copy that came through the `public` channel gets a general caution, unless the publisher itself is open
    access and the copy is from its own page (sources() puts that page first for exactly these rows)."""
    low = (text or "").lower()
    v = next((lab for pat, lab, *head in VERSION_MARKS if re.search(pat, low[:head[0]] if head else low)), "")
    if not v and row is not None and "已下到" in (row.get("tried") or {}).get("public", ""):
        own_page = row.get("bucket") in OA_FRIENDLY and (row.get("is_oa") or row.get("oa_pdf"))
        # a repository often holds the publisher's typeset PDF: it prints the paper's own DOI in its header
        # (2026-11-21 re-test: an INFORMS cover page from PolyU's repository got the caution label)
        printed = bool(row.get("doi")) and row["doi"].lower() in re.sub(r"\s+", "", low[:1500])
        if not own_page and not printed:
            v = "公开版本（非出版商渠道，版本看首页）"
    return v


def pdf_text(path, pages=2):
    import fitz
    d = fitz.open(path)
    n = d.page_count
    t = " ".join(d[k].get_text() for k in range(min(pages, n)))
    d.close()
    return n, t


def casing_from_pdf(title, text):
    """Take the title's letter case from the PDF (the printed version) when metadata is in sentence case.
    Only upgrades (more capitalised words); ignores titles printed in ALL CAPS."""
    toks = re.findall(r"[A-Za-z0-9]+", title)
    if len(toks) < 3:
        return title
    m = re.search(r"[\W_]+".join(re.escape(t) for t in toks), text, re.I)
    if not m:
        return title
    ptoks = re.findall(r"[A-Za-z0-9]+", m.group(0))
    if len(ptoks) != len(toks):
        return title
    if sum(t.isupper() and len(t) > 1 for t in ptoks) > len(ptoks) / 2:
        return title
    if sum(t[:1].isupper() for t in ptoks) <= sum(t[:1].isupper() for t in toks):
        return title
    it = iter(ptoks)
    return re.sub(r"[A-Za-z0-9]+", lambda _: next(it), title)


def title_score(row, ntext):
    words = [w for w in norm(row.get("title", "")).split() if len(w) > 3 or CJK.search(w)]
    if not words:
        return 0.0
    return sum(w in ntext for w in words) / len(words)


def cmd_ingest(a):
    rows = load(a.manifest)
    by_id = {r["id"]: r for r in rows}
    by_doi = {r["doi"]: r for r in rows if r.get("doi")}
    by_arx = {r["arxiv"]: r for r in rows if r.get("arxiv")}
    dest = Path(a.dest)
    since = time.time() - a.since * 3600 if a.since else 0
    files = []
    for s in a.src:
        sp = Path(s).expanduser()
        it = sp.rglob(a.pattern) if a.recursive else sp.glob(a.pattern)
        files += [p for p in it if p.is_file() and p.suffix.lower() == ".pdf" and p.stat().st_mtime >= since and dest not in p.parents]
    merged = merge_results(rows, a.src)
    if merged:
        print(f"merged {merged} result lines from results.jsonl into the tried ledger")
    log = open(Path(a.manifest).with_name("ingest_log.tsv"), "a", encoding="utf-8")
    filed = 0
    for p in sorted(files):
        try:
            if p.read_bytes()[:5] != b"%PDF-":
                print(f"SKIP not-a-pdf  {p}")
                continue
            if not pdf_complete(p):
                print(f"TRUNCATED        {p.name}  缺 %%EOF，下载被截断：删掉重下")
                continue
            n, text = pdf_text(p)
            if n < 1:
                print(f"TRUNCATED        {p.name}  0 页：删掉重下")
                continue
        except Exception as e:
            print(f"SKIP unreadable {p} ({e})")
            continue
        ntext, low = norm(text), text.lower()
        cand = None
        m = re.fullmatch(r"(?:[A-Za-z]+_)?(\d{1,4})", p.stem)          # staging names: 12.pdf / LD_12.pdf
        if m and int(m.group(1)) in by_id:
            # scans have no text layer, so the title score is 0; with --force the operator has
            # already checked the first page, and the staged <id>.pdf is the row's own file
            if title_score(by_id[int(m.group(1))], ntext) >= 0.6 or a.force:
                cand = by_id[int(m.group(1))]
        if not cand:
            # every listed DOI printed in the text, in print order; the best title wins, an earlier one on a tie.
            # (A set here made the pick depend on hash order: a paper citing another row's DOI could be filed there.)
            found, seen = [], set()
            for x in DOI_RE.findall(low):
                d = clean_doi(x)
                if d in seen or d not in by_doi:
                    continue
                seen.add(d)
                s = title_score(by_doi[d], ntext)
                if s >= 0.3:
                    found.append((s, -len(found), by_doi[d]["id"]))
            if found:
                cand = by_id[max(found)[2]]
        if not cand:
            for x in re.findall(r"arxiv:\s*(\d{4}\.\d{4,5})", low):
                if x in by_arx:
                    cand = by_arx[x]
                    break
        if not cand:
            scored = sorted(((title_score(r, ntext), r["id"]) for r in rows), reverse=True)
            if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
                cand = by_id[scored[0][1]]
        if not cand:
            print(f"NOMATCH          {p.name}  (pages={n}) first text: {text[:80].strip()!r}")
            continue
        r = cand
        ts = title_score(r, ntext)
        # the DOI is often broken over two lines in the extracted text ("10.1287/mksc." + newline + "1070.0303")
        doi_in = bool(r.get("doi")) and (r["doi"] in low or r["doi"] in re.sub(r"\s+", "", low))
        sur = norm(r.get("surname", ""))
        author_in = bool(sur) and sur in ntext
        flags = []
        if n < a.min_pages:
            flags.append(f"只有{n}页" + ("（SSRN、知识库上的 1–2 页文件多半只是摘要页或封面）" if n <= 2 else ""))
        if ts < 0.6 and not doi_in:
            flags.append(f"标题匹配{ts:.0%}")
        if not author_in:
            flags.append("首两页没找到第一作者姓")
        other = [] if doi_in else other_dois(text, r.get("doi"))
        if other:
            # 2026-11-21: a 2004 comment on a 1997 paper scored title=100% (its title contains the paper's) and was
            # filed. The DOI printed at the top of page 1 is the article's own; when it is another one, stop.
            flags.append(f"首页印的是另一个 DOI（{other[0]}），是另一篇文章或另一次刊印（评论、答复、勘误、重印），打开首页核对")
        version = version_of(low)
        blocking = [f for f in flags if not f.startswith("首两页")]
        if blocking and not a.force:
            print(f"SUSPECT  #{r['id']:<4} {p.name}  {'; '.join(flags)}  -> not filed (use --force after checking)")
            continue
        h = sha256(p)
        if r["status"] in ("done", "have") and r.get("sha256") == h:
            print(f"DUP      #{r['id']:<4} {p.name}  identical to {r['file']}")
            continue
        if r["status"] in ("done", "have") and not a.replace:
            print(f"EXISTS   #{r['id']:<4} {p.name}  row already has {r['file']} (pass --replace to overwrite)")
            continue
        full = r.get("title", "") + ((": " + r["subtitle"]) if r.get("subtitle") else "")
        fixed = casing_from_pdf(full, text) if full else full
        if fixed != full:
            r["title"], r["subtitle"] = fixed, ""
            r["target"] = target_name(r)
            flags.append("标题大小写按 PDF 修正")
        tdir = dest / r["subdir"] if r.get("subdir") else dest
        name = safe_filename(r["target"] or p.name)     # older manifests may hold a name Windows refuses
        tgt = tdir / name
        if len(str(tgt)) > MAX_PATH:
            keep = MAX_PATH - len(str(tdir)) - 6
            name = name[:keep].rstrip(" .-") + ".pdf"
            tgt = tdir / name
            flags.append("路径超长已截断标题")
        if a.dry_run:
            print(f"DRYRUN   #{r['id']:<4} {p.name} -> {tgt}  pages={n} title={ts:.0%} doi={doi_in} {version} {'; '.join(flags)}")
            continue
        try:
            tdir.mkdir(parents=True, exist_ok=True)
            (shutil.move if a.move else shutil.copy2)(str(p), str(tgt))
        except OSError as e:                            # one bad file must not lose the rows filed before it
            print(f"ERROR    #{r['id']:<4} {p.name} -> {name}  没能写进库里：{str(e)[:120]}")
            continue
        version = version or version_of(low, r)
        r.update(status="done", file=str(tgt), pages=n, sha256=h, version=version or "未见预印本标记", source=str(p))
        show_stuck(r)                                   # the file is in: nothing is left open
        if flags:
            r["note"] = "; ".join(x for x in [r.get("note", ""), *flags] if x)
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{r['id']}\t{'move' if a.move else 'copy'}\t{p}\t{tgt}\n")
        filed += 1
        print(f"FILED    #{r['id']:<4} {name}  pages={n} title={ts:.0%} doi={doi_in} {version} {'; '.join(flags)}")
    log.close()
    if not a.dry_run:
        save(a.manifest, rows)
        export_xlsx(a.manifest, rows)
    print(f"\nfiled {filed} of {len(files)} files")
    summary(rows)


# --------------------------------------------------------------------------- alts: public copies via OpenAlex
SUPP = re.compile(r"_sm\d+|suppl|supporting|appendix|_si[_.]|/media/", re.I)


def cmd_alts(a):
    rows = load(a.manifest)
    want = set(int(x) for x in a.ids.split(",")) if a.ids else None
    todo = [r for r in rows if r["status"] == "todo" and r.get("doi") and (want is None or r["id"] in want)]
    found = 0
    for r in todo:
        time.sleep(random.uniform(0.4, 1.0))
        w = http_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(r["doi"], safe="/()"))
        if w is None:
            print(f"{r['id']:>4} OpenAlex 查不到，没记账")
            continue
        own = r["doi"].lower()
        urls, kinds = [], []
        for loc in w.get("locations") or []:
            src = (loc.get("source") or {})
            for u in (loc.get("pdf_url"), loc.get("landing_page_url")):
                if not u or own in u.lower() or u in urls or SUPP.search(u):
                    continue
                if src.get("type") == "journal" and not loc.get("is_oa"):
                    continue                             # the publisher's own paywalled page is not a public copy
                urls.append(u)
                kinds.append(f"{src.get('display_name') or urllib.parse.urlsplit(u).netloc}·{loc.get('version') or '?'}")
        r["alt_copies"] = urls
        if urls:
            found += 1
            r["alt_note"] = "；".join(dict.fromkeys(kinds))[:200]
            print(f"{r['id']:>4} {len(urls)} 个公开版本：{r['alt_note'][:90]}")
        else:
            add_tried(r, "public", f"OpenAlex 没有公开版本（{time.strftime('%Y-%m-%d')}）")
    save(a.manifest, rows)
    export_xlsx(a.manifest, rows)
    print(f"\n{found}/{len(todo)} 篇有公开版本（写在 alt_copies；Figshare 上的常是补充材料，下回来要看首页）。"
          f"下一步：litbrowser fetch <manifest> --via public --ids …")


# --------------------------------------------------------------------------- mark: the one way to hand-edit rows
def cmd_mark(a):
    rows = load(a.manifest)
    want = set(int(x) for x in a.ids.split(","))
    n = 0
    for r in rows:
        if r["id"] not in want:
            continue
        n += 1
        if a.status:
            r["status"] = a.status
        if a.route:
            r["route"] = a.route
        if a.version:
            r["version"] = a.version
        if a.note:
            r["note"] = "; ".join(x for x in [r.get("note", ""), a.note] if x)
        for t in a.tried or []:
            ch, _, text = t.partition("=")
            if ch not in CHANNELS + ("ablesci",):
                sys.exit(f"--tried 的渠道只能是 {CHANNELS + ('ablesci',)}，收到 {ch!r}")
            add_tried(r, ch, text or f"试过（{time.strftime('%Y-%m-%d')}）")
            r.setdefault("res_at", {})[ch] = time.strftime("%Y-%m-%d %H:%M:%S")     # older result lines no longer apply
            (r.get("stuck") or {}).pop(ch, None)                                    # this channel has its answer now
            show_stuck(r)
        for ch in a.untried or []:
            (r.get("tried") or {}).pop(ch, None)
            r.setdefault("res_at", {})[ch] = time.strftime("%Y-%m-%d %H:%M:%S")     # older result lines no longer apply
        if a.clear_pending:
            open_chs = list(r.get("stuck") or {}) or [(r.get("pending") or "").split("：")[0]]
            for ch in filter(None, open_chs):
                r.setdefault("res_at", {})[ch] = time.strftime("%Y-%m-%d %H:%M:%S")
            r.pop("stuck", None)
            r.pop("pending", None)
    save(a.manifest, rows)
    export_xlsx(a.manifest, rows)
    print(f"updated {n} rows (manifest + xlsx)")


def cmd_rename(a):
    """journal_abbr.json gained or changed an abbreviation (the user agreed it): give the rows of this manifest the
    abbreviation the table now holds. Only the "(YY ABBR)" part of a name changes; the title as filed (casing taken
    from the PDF, a title shortened for the path limit) stays. Filed rows are renamed on disk and logged in
    ingest_log.tsv; rows not yet filed only get the new target name. 2026-11-22: CA -> CACM after the user agreed."""
    rows = load(a.manifest)
    amap = load_abbr()
    want = set(int(x) for x in a.ids.split(",")) if a.ids else None
    log = None if a.dry_run else open(Path(a.manifest).with_name("ingest_log.tsv"), "a", encoding="utf-8")
    tag = "DRYRUN  " if a.dry_run else ""
    n_name = n_flag = 0
    still = set()
    for r in rows:
        if want is not None and r["id"] not in want:
            continue
        old, journal = r.get("abbr") or "", r.get("journal") or ""
        if not journal or old == "arXiv" or CJK.search(journal):
            continue
        new, guessed = guess_abbr(journal, amap)
        if guessed:
            if r.get("abbr_guess"):
                still.add((journal, old))
            continue
        if new == old:                                   # the table now confirms what was guessed
            if r.get("abbr_guess"):
                print(f"{tag}CONFIRMED #{r['id']:<4} {journal} -> {new}")
                if not a.dry_run:
                    r["abbr_guess"] = False
                n_flag += 1
            continue
        token = re.compile(r"\((\w{2}) " + re.escape(BAD_CHARS.sub("", old)) + r"\)")
        swap = lambda s: token.sub(lambda m: f"({m.group(1)} {BAD_CHARS.sub('', new)})", s or "", count=1)
        p = Path(r["file"]) if r.get("file") else None
        q = p.with_name(swap(p.name)) if p else None
        if p and r.get("status") in ("done", "have") and q.name == p.name:
            print(f"{tag}KEPT     #{r['id']:<4} {p.name}: no \"({old})\" in the file name; only the row changes")
        elif p and r.get("status") in ("done", "have"):
            if not p.exists():
                print(f"MISSING  #{r['id']:<4} {p}  file not on disk; the row keeps its old name")
                continue
            if q.exists():
                print(f"CLASH    #{r['id']:<4} {q.name} already exists; not renamed")
                continue
            if len(str(q)) > MAX_PATH:
                print(f"TOOLONG  #{r['id']:<4} {q}  over {MAX_PATH} characters; not renamed")
                continue
            print(f"{tag}RENAMED  #{r['id']:<4} {p.name} -> {q.name}")
            if not a.dry_run:
                p.rename(q)
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{r['id']}\trename\t{p}\t{q}\n")
                r["file"] = str(q)
        else:
            print(f"{tag}ABBR     #{r['id']:<4} {old} -> {new}  (not filed yet: only the target name changes)")
        if not a.dry_run:
            r["abbr"], r["abbr_guess"], r["target"] = new, False, swap(r.get("target")) or r.get("target", "")
        n_name += 1
    if log:
        log.close()
    if not a.dry_run and (n_name or n_flag):
        save(a.manifest, rows)
        export_xlsx(a.manifest, rows)
    print(f"{'would change' if a.dry_run else 'changed'} {n_name} rows' abbreviation, confirmed {n_flag} guessed ones")
    for journal, ab in sorted(still):
        print(f"  still a guess (not in journal_abbr.json): {journal} -> {ab}")


def cmd_merge(a):
    """Fold results.jsonl into the tried ledger without filing anything (a round that brought no file still has answers)."""
    rows = load(a.manifest)
    n = merge_results(rows, a.src)
    save(a.manifest, rows)
    export_xlsx(a.manifest, rows)
    print(f"merged {n} result lines into the tried ledger (manifest + xlsx)")
    left = [r for r in rows if r.get("pending")]
    for r in left:
        print(f"  未了结 #{r['id']:<4} {r['pending'][:110]}")


# --------------------------------------------------------------------------- config: the user's own school
def set_env(pairs):
    """KEY=VALUE into ~/.litdl/.env: a key's old line is replaced, every other line stays as it is."""
    path = LITDL_HOME / ".env"
    LITDL_HOME.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for k, v in pairs.items():
        lines = [ln for ln in lines if not re.match(rf"\s*{re.escape(k)}\s*=", ln)] + [f"{k}={v}"]
        os.environ[k] = v
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cmd_config(a):
    """Show, or fill in, what belongs to the user's own school. Run first in a new place: without a WebVPN host
    `plan`, `fetch` and `login webvpn` stop and ask for it."""
    new = {}
    if a.webvpn is not None:
        v = re.sub(r"^https?://", "", a.webvpn.strip()).split("/")[0]
        if v.lower() not in NO_VPN and not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?", v):
            sys.exit(f"{a.webvpn!r} 不像一个域名。要的是学校 WebVPN 门户的主机名，形如 webvpn.xxx.edu.cn；没有就填 none。")
        new["LITDL_WEBVPN"] = v
    if a.ebsco_opid is not None:
        new["LITDL_EBSCO_OPID"] = a.ebsco_opid.strip()
    if new:
        print(f"已写入 {set_env(new)}：" + "、".join(f"{k}={v}" for k, v in new.items()))
    vpn, opid = (os.environ.get("LITDL_WEBVPN") or "").strip(), (os.environ.get("LITDL_EBSCO_OPID") or "").strip()
    print("学校 WebVPN：" + ("没有（填的是 none，经 WebVPN 的库都不排）" if vpn.lower() in NO_VPN else vpn or "还没填"))
    print("EBSCO opid：" + (opid or "还没填（只有走 EBSCO 时才要；怎么找见 references/setup.md）"))
    files = load_holdings()["files"]
    print("收录表：" + (" ← ".join(reversed(files)) if files else "脚本内置的起步表") + "（靠前的盖过靠后的）")
    if not vpn:
        print("NEED_USER  先问用户学校 WebVPN 门户的域名，再跑：litdl.py config --webvpn <域名>（没有学校 WebVPN 填 none）")
        return 2
    return 0


# --------------------------------------------------------------------------- locate: which database, before trying
# how a publisher's name shows up in a library's resource list
PUB_WORDS = {"informs": r"informs", "sage": r"\bsage\b", "tandf": r"taylor", "emerald": r"emerald", "acm": r"\bacm\b",
             "wiley": r"wiley", "springer": r"springer", "elsevier": r"elsevier|sciencedirect", "ieee": r"\bieee\b",
             "nature": r"nature", "science": r"science online|sciencemag", "pnas": r"pnas", "oup": r"oxford",
             "cambridge": r"cambridge"}
# free platforms that search across publishers
FREE_FINDERS = r"一站式|发现平台|open access library|directory of open access|oalib|pubscholar|公益学术|^nstl|国际学术资源|pubmed|doaj|openalex"
_LIB = None

# A record whose title is the wanted title plus words like these is a different article about it: 2026-11-21 EBSCO
# gave "Comments on 'Information Distortion in a Supply Chain: The Bullwhip Effect'" (2004) for the 1997 paper,
# because the old rule took any title that *contained* the wanted one.
ABOUT_IT = re.compile(r"\b(comments?|commentary|reply|replies|response|rejoinder|erratum|errata|corrigendum|correction|"
                      r"retraction|retracted|reflections?|discussion|review of|book review|editorial|introduction to|"
                      r"note on|addendum)\b")
# words that may stand in front of a title in a database without making it another article
PREFIX_OK = re.compile(r"^(technical note|research note|note|short communication|research article|article|letter)\s+")


def pick_title(want, records):
    """-> the record whose normalised title `t` is this paper's, or None. An exact match wins; otherwise a record whose
    title only adds a harmless prefix ("Technical Note—…") or is the wanted title cut short. A longer title that
    contains the wanted one is taken only when the extra words carry no sign of being a comment, reply, erratum…"""
    exact = [d for d in records if d["t"] == want]
    if exact:
        return exact[0]
    for d in records:
        t = d["t"]
        if want in t:
            extra = t.replace(want, " ").strip()
            if PREFIX_OK.sub("", t).strip() == want:
                return d
            if extra and not ABOUT_IT.search(extra) and len(extra.split()) <= 3:
                return d
        elif len(t) > 25 and want.startswith(t):
            return d                             # the database cut the title (subtitle dropped)
    return None


def library_resources():
    """What `litbrowser.py portal --library --save` read off the school library's resource list:
    [{name, url, status: 已购 / 试用 / 免费}], or [] when that page has not been read."""
    global _LIB
    if _LIB is None:
        try:
            _LIB = json.loads((LITDL_HOME / "portal.json").read_text(encoding="utf-8")).get("resources") or []
        except (OSError, ValueError):
            _LIB = []
    return _LIB


def cmd_locate(a):
    """Print, per paper still to get, which databases can hold it and which one is next. Sends no request: the
    judgment comes from the publisher (DOI prefix), the journal, the open-access flag and scripts/holdings.json."""
    from collections import Counter
    rows = load(a.manifest)
    want = set(int(x) for x in a.ids.split(",")) if a.ids else None
    nxt = Counter()
    for r in rows:
        if (want is not None and r["id"] not in want) or (not a.all and r["status"] in ("done", "have")):
            continue
        s = next_source(r)
        if not sources(r):
            step = ROUTE.get(r.get("bucket"), ROUTE["other"])
        elif s:
            step = SOURCE_CN[s]
        else:
            step = "能收录它的库都试过了（可以去科研通）"
        nxt[step] += 1
        yr = r.get("year_print") or r.get("year_online") or ""
        name = (r.get("target") or r.get("doi") or "")[:70]
        if sources(r):
            print(f"#{r['id']:<4} {r.get('abbr', ''):6s} {yr}  {route_text(r)}\n      下一步：{step}  {name}")
        else:                                        # arXiv, CNKI, no metadata: there is no order of databases to show
            print(f"#{r['id']:<4} {r.get('abbr', ''):6s} {yr}  {step}  {name}")
        word = PUB_WORDS.get(r.get("bucket"))
        for x in library_resources() if word and r["status"] not in ("done", "have") else []:
            if x.get("status") in ("免费", "试用") and re.search(word, x.get("name", ""), re.I):
                print(f"      图书馆资源页里对口的：[{x['status']}] {x['name'][:50]}  {x.get('url', '')[:80]}")
        if a.save and sources(r) and r["status"] == "todo":
            r["route"] = route_text(r)
    print("\n下一步按库分：" + "；".join(f"{k} {v} 篇" for k, v in nxt.most_common()))
    lib = library_resources()
    if lib:
        finders = [r for r in lib if r.get("status") == "免费" and re.search(FREE_FINDERS, r.get("name", ""), re.I)]
        if finders:
            print("图书馆列的免费检索平台（上面的库都没有时，按题名或 DOI 去查；没有现成配方，用 litbrowser 的原语探）：")
            for r in finders:
                print(f"    {r['name'][:46]}  {r.get('url', '')[:80]}")
    else:
        print("还没读过学校图书馆的资源列表（那里常比 WebVPN 首页全，还单列免费资源）：litbrowser.py portal --library --save")
    if a.save:
        save(a.manifest, rows)
        export_xlsx(a.manifest, rows)
        print("建议渠道一栏已更新（manifest + xlsx）")


# --------------------------------------------------------------------------- missing / summary
def summary(rows):
    from collections import Counter
    st = Counter(r["status"] for r in rows)
    print("\n" + " | ".join(f"{STATUS_CN.get(k, k)} {v}" for k, v in st.items()) + f" | 共 {len(rows)}")
    left = Counter(r["bucket"] for r in rows if r["status"] not in ("done", "have"))
    if left:
        print("未完成按桶: " + ", ".join(f"{k} {v}" for k, v in left.most_common()))


def cmd_missing(a):
    rows = load(a.manifest)
    left = [r for r in rows if r["status"] not in ("done", "have")]
    summary(rows)
    if not left:
        print("\n全部到位。")
        return
    if a.exhausted:                                    # rows that may go to 科研通: every database that can hold the
        def lack(r):                                   # paper (required()) has an entry in the ledger
            return [c for c in required(r) if c not in (r.get("tried") or {})]
        ex = [r for r in left if required(r) and not lack(r)]
        none_ = [r for r in left if not required(r)]
        part = [r for r in left if r not in ex and r not in none_]
        asked = [r for r in ex if "ablesci" in (r.get("tried") or {})]
        new = len(ex) - len(asked)
        print(f"\n能收录它的库都试过仍没有的：{len(ex)} 篇；其中 {new} 篇还没发过求助，按每篇 {a.points} 积分算，"
              f"科研通求助共需 {new * a.points} 积分（发之前要用户同意）。")
        for r in asked:
            print(f"  #{r['id']:<4} 科研通：{r['tried']['ablesci'][:70]}")
        print(f"还有库没试的：{len(part)} 篇，不能去科研通：")
        for r in part:
            print(f"  #{r['id']:<4} 缺 {'、'.join(lack(r))}  {r['target'][:70]}")
        if none_:
            print(f"没有可试的库：{len(none_)} 篇（arXiv 的行用 litdl arxiv；中文的走知网；元数据查不到的先核对 DOI）：")
            for r in none_:
                tag = r.get("target") or r.get("doi") or r.get("arxiv") or ""
                print(f"  #{r['id']:<4} {ROUTE.get(r.get('bucket'), '')}  {tag}"[:160])
        left = ex
        if not left:
            return
    lines = ["| # | 文献（完整标题） | 期刊 年份 | 链接 | 建议渠道 | 已试渠道 | 下载后文件名 |", "|---|---|---|---|---|---|---|"]
    for r in sorted(left, key=lambda r: (r["route"], r["id"])):
        full = (r["title"] + ((": " + r["subtitle"]) if r.get("subtitle") else "")) or f"（查不到题名：{r.get('doi') or r.get('arxiv') or ''}）"
        link = f"https://doi.org/{r['doi']}" if r.get("doi") else f"https://arxiv.org/abs/{r.get('arxiv', '')}"
        yr = r.get("year_print") or r.get("year_online") or ""
        sub = (r["subdir"] + "/") if r.get("subdir") else ""
        tried = tried_text(r) + (f"；未了结：{r['pending']}" if r.get("pending") else "")
        route = route_text(r) if sources(r) and r["status"] == "todo" else r["route"]      # live: ticks follow the ledger
        lines.append(f"| {r['id']} | {full} | {r.get('abbr', '')} {yr} | {link} | {route} | {tried} | `{sub}{r['target']}` |")
    txt = "\n".join(lines)
    print("\n" + txt)
    if a.write:
        Path(a.write).write_text(f"# 待手动下载（{len(left)} 篇）\n\n下载后放到项目根目录或“下载”文件夹即可，不用改名。\n\n{txt}\n", encoding="utf-8")
        print(f"\n-> {a.write}")


# --------------------------------------------------------------------------- CLI
def add_pacing(p):
    p.add_argument("--delay", type=float, default=3, help="base gap between papers in seconds; the real wait is jittered and adapts (see Pacer)")
    p.add_argument("--max-delay", type=float, default=300, help="ceiling for the gap after repeated failures")
    p.add_argument("--rest-every", type=int, default=8, help="take a longer rest after every N papers (0 = never)")
    p.add_argument("--stop-after", type=int, default=3, help="stop after N consecutive failures; a 403/429/503 counts double (0 = never stop)")


def main():
    global WEBVPN_DEFAULT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("plan", help="identifiers -> manifest.json/.xlsx")
    p.add_argument("input", help="txt (one DOI/arXiv per line, optional TAB subfolder) or xlsx/csv with a DOI column")
    p.add_argument("--out", required=True, help="manifest.json path")
    p.add_argument("--col", help="DOI column header (xlsx/csv)")
    p.add_argument("--subdir-col", help="column holding the destination subfolder (xlsx/csv)")
    p.add_argument("--have", action="append", help="existing library folder; matching titles are marked 库中已有 (repeatable)")
    p.add_argument("--webvpn", default=WEBVPN_DEFAULT,
                   help="school WebVPN host for rewritten links (default: what `config --webvpn` saved; 'none' = no WebVPN)")

    p = sp.add_parser("vpnfetch", help="IEEE/Springer via WebVPN + curl")
    p.add_argument("manifest")
    p.add_argument("--stage", required=True)
    p.add_argument("--cookies", default=DEFAULT_COOKIES)
    p.add_argument("--from-browser", action="store_true", help="take the WebVPN cookies from the running litbrowser Chrome instead of the scansci cookie file")
    p.add_argument("--webvpn", default=WEBVPN_DEFAULT)
    p.add_argument("--marker", default="sign_out", help="text present on the portal page when logged in")
    p.add_argument("--ids", help="comma-separated row ids (default: all todo ieee/springer rows)")
    add_pacing(p)
    p.add_argument("--dry-run", action="store_true")

    p = sp.add_parser("arxiv", help="arXiv PDFs (stdlib only; direct connection unless --proxy is given)")
    p.add_argument("manifest", nargs="?")
    p.add_argument("--stage")
    p.add_argument("--proxy", help="only when the user asks for it: e.g. http://127.0.0.1:10809. Default is a direct "
                                   "connection that ignores the system proxy")
    add_pacing(p)
    p.add_argument("--probe", action="store_true", help="speed test only")

    p = sp.add_parser("ingest", help="verify + name + file PDFs")
    p.add_argument("manifest")
    p.add_argument("--src", action="append", required=True, help="folder to scan (repeatable)")
    p.add_argument("--dest", required=True, help="library root; row subdir is appended")
    p.add_argument("--pattern", default="*.pdf")
    p.add_argument("--since", type=float, help="only files modified in the last N hours")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--move", action="store_true", help="move instead of copy (use for staging folders, not for the user's originals)")
    p.add_argument("--min-pages", type=int, default=3)
    p.add_argument("--force", action="store_true", help="file even if suspect")
    p.add_argument("--replace", action="store_true", help="overwrite a row that already has a file")
    p.add_argument("--dry-run", action="store_true")

    p = sp.add_parser("missing", help="summary + hand-over table")
    p.add_argument("manifest")
    p.add_argument("--write", help="also write the table to this .md file")
    p.add_argument("--exhausted", action="store_true",
                   help="only rows whose ledger has an entry for every database that can hold the paper (see locate)")
    p.add_argument("--points", type=int, default=10, help="科研通 points per request, for the total shown with --exhausted")

    p = sp.add_parser("config", help="show or fill in the user's own school: WebVPN host, EBSCO customer id")
    p.add_argument("--webvpn", help="host of the school's WebVPN portal, e.g. webvpn.xxx.edu.cn; 'none' if there is none")
    p.add_argument("--ebsco-opid", help="the <opid> in https://research-ebsco-com-443.<vpn>/c/<opid>/search")

    p = sp.add_parser("locate", help="which databases can hold each missing paper, and which one is next (no request sent)")
    p.add_argument("manifest")
    p.add_argument("--ids")
    p.add_argument("--all", action="store_true", help="also rows that are already downloaded")
    p.add_argument("--save", action="store_true", help="write the order into the manifest's route column (and the xlsx)")

    p = sp.add_parser("alts", help="public copies of missing rows (OpenAlex locations)")
    p.add_argument("manifest")
    p.add_argument("--ids")

    p = sp.add_parser("merge", help="fold <stage>/results.jsonl into the tried ledger (no files needed)")
    p.add_argument("manifest")
    p.add_argument("--src", action="append", required=True, help="stage folder with results.jsonl (repeatable)")

    p = sp.add_parser("mark", help="edit rows and refresh the xlsx")
    p.add_argument("manifest")
    p.add_argument("--ids", required=True)
    p.add_argument("--status", choices=["todo", "done", "have", "manual"])
    p.add_argument("--route")
    p.add_argument("--note", help="appended to the row's note")
    p.add_argument("--version")
    p.add_argument("--tried", action="append", help="channel=text, channel one of ebsco / publisher / scihub / public / ablesci (repeatable)")
    p.add_argument("--clear-pending", action="store_true")
    p.add_argument("--untried", action="append", help="channel whose ledger entry is wrong and goes (repeatable); e.g. a publisher PDF that was recorded as public")

    p = sp.add_parser("rename", help="after the user agreed a new abbreviation: apply journal_abbr.json to filed rows")
    p.add_argument("manifest")
    p.add_argument("--ids", help="only these rows (default: all)")
    p.add_argument("--dry-run", action="store_true", help="list what would change, touch nothing")

    a = ap.parse_args()
    if getattr(a, "webvpn", None) is not None and a.cmd != "config":
        WEBVPN_DEFAULT = a.webvpn.strip()        # this command's --webvpn also decides which databases are tried
    if a.cmd == "arxiv" and not a.probe and not (a.manifest and a.stage):
        ap.error("arxiv needs manifest and --stage (or --probe)")
    return {"plan": cmd_plan, "vpnfetch": cmd_vpnfetch, "arxiv": cmd_arxiv, "ingest": cmd_ingest, "missing": cmd_missing,
            "alts": cmd_alts, "mark": cmd_mark, "rename": cmd_rename, "merge": cmd_merge, "locate": cmd_locate,
            "config": cmd_config}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
