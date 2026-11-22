#!/usr/bin/env python3
"""Optional AI assist for CAPTCHA / human-verification pages in litbrowser.

The user asked for this on 2026-11-19 (see references/pitfalls.md #6). It is only used when a
vision model is configured; without a key it is inert and litbrowser waits for the user as before.

Handles the two widget shapes that show up on publisher sites and school WebVPN portals:
  * click / point-select  - read the instruction, click characters or image tiles in order
  * slider / jigsaw       - drag the knob so the puzzle piece fills the gap

It never types passwords, reads SMS codes or touches account data. Every attempt is bounded and
the caller falls back to waiting for the user when it fails.

Config (environment, or KEY=VALUE lines in ~/.litdl/.env):
  LITDL_CAPTCHA_API_KEY   or OPENAI_API_KEY
  LITDL_CAPTCHA_BASE_URL  or OPENAI_BASE_URL   (default https://api.openai.com/v1)
  LITDL_CAPTCHA_MODEL     or OPENAI_MODEL      (default gpt-4o-mini)
  LITDL_CAPTCHA_ATTEMPTS  (default 3, max 6)
  LITDL_CAPTCHA_TIMEOUT   (seconds, default 60)
  LITDL_CAPTCHA=0         turn the whole thing off

Only stdlib is used. In the default (hands-off) path litbrowser lets go of the browser first, takes the
screenshot over a bare DevTools connection and clicks with the system mouse; it only borrows `plan_shot`
and `_checkbox_point` from here. `solve` below is the older attached path (LITDL_HANDSOFF=0, or no
`websockets` package): mouse input through Playwright, which dispatches trusted CDP
Input.dispatchMouseEvent calls.
"""
import base64
import json
import math
import os
import random
import re
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

_DEFAULT_HOME = Path(os.environ.get("LITDL_HOME") or (Path.home() / ".litdl"))
_STATE = {"enabled": None, "attempts": None}       # None = follow env / config file

PLAN_SYSTEM = """You are a CAPTCHA assistant controlling a real Chrome window. The image is the current viewport.
Coordinates must be pixels in THIS image. Reply with JSON only - no markdown, no prose.

Classify the verification widget and return exactly one of:
{"type":"slider","handle":[x1,y1,x2,y2],"piece":[x1,y1,x2,y2]|null,"gap":[x1,y1,x2,y2],"note":""}
  handle: the draggable knob on the track below the puzzle.
  piece:  the puzzle piece that must be moved (usually at the left edge of the puzzle image);
          null when it is not visible.
  gap:    the notch/hole the piece must fill.
{"type":"click","points":[[x,y],...],"submit":[x,y]|null,"note":""}
  points: every character/word/image-tile to click, in the exact order the instruction asks.
  submit: the confirm button if one must be pressed after the picks, else null.
{"type":"checkbox","points":[[x,y]],"submit":null,"note":""}
  a single "I am not a robot" checkbox (Cloudflare Turnstile / reCAPTCHA / hCaptcha):
  one click, on the square at the LEFT edge of the widget, vertically centred.
{"type":"none","note":"why"}
  no verification widget is visible, or its instruction cannot be read.

Read the instruction inside the widget carefully (it may be Chinese or English and rendered as an
image). Do not invent a widget that is not on screen."""

PLAN_USER = """Page: {title}
URL: {url}
Viewport: {w}x{h} px
Visible text (may be truncated and may not include text drawn inside the widget):
{body}

Return the JSON plan."""

VERIFY_SYSTEM = """You judge a browser screenshot taken right after an action on a CAPTCHA.
Reply JSON only: {"solved": true|false, "reason": "short"}
solved=true only when the widget is gone or visibly reports success (check mark, "验证成功",
"Success", "通过"). A widget still asking for input is false."""

VERIFY_USER = "This is the screenshot after the action. Was the verification passed?"


# --------------------------------------------------------------------------- config
def load_env(home=None):
    """Read KEY=VALUE lines from ~/.litdl/.env. Real environment variables win."""
    path = Path(home or _DEFAULT_HOME) / ".env"
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
        if k and v:
            os.environ.setdefault(k, v)


def set_enabled(value):
    _STATE["enabled"] = None if value is None else bool(value)


def set_attempts(value):
    _STATE["attempts"] = None if value in (None, 0) else int(value)


def config(home=None):
    load_env(home)
    key = os.environ.get("LITDL_CAPTCHA_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    base = (os.environ.get("LITDL_CAPTCHA_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LITDL_CAPTCHA_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    off = str(os.environ.get("LITDL_CAPTCHA", "")).strip().lower()
    enabled = _STATE["enabled"] if _STATE["enabled"] is not None else off not in ("0", "off", "false", "no")
    attempts = _STATE["attempts"]
    if attempts is None:
        try:
            attempts = int(os.environ.get("LITDL_CAPTCHA_ATTEMPTS", "3"))
        except ValueError:
            attempts = 3
    attempts = max(1, min(6, attempts))
    try:
        timeout = float(os.environ.get("LITDL_CAPTCHA_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0
    return {"ready": bool(key) and enabled, "enabled": enabled, "api_key": key,
            "base_url": base, "model": model, "attempts": attempts, "timeout": timeout}


# --------------------------------------------------------------------------- model call
def _extract_json(content):
    if not isinstance(content, str):
        return None
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def _ai_json(messages, cfg, max_tokens=700):
    payload = json.dumps({"model": cfg["model"], "temperature": 0, "max_tokens": max_tokens,
                          "messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return None, f"HTTP {e.code} {detail}".strip()
    except Exception as e:
        return None, str(e)[:200]
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None, "返回格式不认识"
    if isinstance(content, list):                    # some gateways return content as parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return _extract_json(content), None


# --------------------------------------------------------------------------- screenshot
def _jpeg_size(buf):
    i = 2
    while i < len(buf) - 9:
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = (buf[i + 5] << 8) | buf[i + 6]
            w = (buf[i + 7] << 8) | buf[i + 8]
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = (buf[i + 2] << 8) | buf[i + 3]
        i += 2 + seg
    return None


def _image_size(buf):
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", buf[16:24])
    if buf[:2] == b"\xff\xd8":
        return _jpeg_size(buf)
    return None


def _shot(page):
    """Screenshot the viewport as JPEG, plus the scale from image pixels to CSS pixels."""
    raw = None
    try:
        raw = page.screenshot(type="jpeg", quality=80, scale="css", timeout=20000)
    except TypeError:                               # older Playwright: no scale kwarg
        try:
            raw = page.screenshot(type="jpeg", quality=80, timeout=20000)
        except Exception:
            raw = page.screenshot(timeout=20000)
    except Exception:
        raw = page.screenshot(timeout=20000)
    try:
        css = page.evaluate("() => ({w: Math.round(window.innerWidth), h: Math.round(window.innerHeight)})")
    except Exception:
        css = {}
    return shot_from_bytes(raw, css.get("w"), css.get("h"))


def shot_from_bytes(raw, css_w=None, css_h=None):
    """The screenshot record the planner works with, from image bytes and the viewport's CSS size.
    Used by litbrowser's hands-off path, which takes the screenshot over a bare DevTools connection."""
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    iw, ih = _image_size(raw) or (0, 0)
    cw = css_w or iw or 1
    ch = css_h or ih or 1
    return {"b64": base64.b64encode(raw).decode("ascii"), "mime": mime,
            "iw": iw, "ih": ih, "cw": cw, "ch": ch,
            "sx": (cw / iw) if iw else 1.0, "sy": (ch / ih) if ih else 1.0}


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


# --------------------------------------------------------------------------- plan normalisation
def _center(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _scaled_bbox(value, shot):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return [x1 * shot["sx"], y1 * shot["sy"], x2 * shot["sx"], y2 * shot["sy"]]


def _scaled_point(value, shot):
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 2:
        try:
            return [float(value[0]) * shot["sx"], float(value[1]) * shot["sy"]]
        except (TypeError, ValueError):
            return None
    box = _scaled_bbox(value, shot)
    return list(_center(box)) if box else None


def _inside(point, shot, margin=4):
    """Guard against a hallucinated coordinate: it must land in the visible viewport."""
    return bool(point) and -margin <= point[0] <= shot["cw"] + margin and -margin <= point[1] <= shot["ch"] + margin


def _normalise(plan, shot):
    if not isinstance(plan, dict):
        return None
    kind = str(plan.get("type") or "").lower()
    if kind == "slider":
        handle = _scaled_bbox(plan.get("handle"), shot)
        gap = _scaled_bbox(plan.get("gap"), shot)
        if not handle or not gap:
            return None
        if not (_inside(_center(handle), shot) and _inside(_center(gap), shot)):
            return None
        piece = _scaled_bbox(plan.get("piece"), shot)
        if piece and not _inside(_center(piece), shot):
            piece = None
        return {"type": "slider", "handle": handle, "gap": gap, "piece": piece,
                "note": str(plan.get("note") or "")}
    if kind in ("click", "checkbox"):
        points = [_scaled_point(p, shot) for p in (plan.get("points") or [])]
        points = [p for p in points if _inside(p, shot)]
        submit = _scaled_point(plan.get("submit"), shot)
        submit = submit if _inside(submit, shot) else None
        if kind == "checkbox" and not points:
            return None
        return {"type": kind if points else "none", "points": points, "submit": submit,
                "note": str(plan.get("note") or "")}
    return {"type": "none", "note": str(plan.get("note") or "")}


# --------------------------------------------------------------------------- known checkbox widgets
# Cloudflare Turnstile, reCAPTCHA and hCaptcha all put a small checkbox near the left edge of a
# cross-origin iframe. Reading that iframe's rect is far more reliable than asking a model to guess
# the coordinate of a 20 px box, so this runs before the vision pass whenever one is on screen.
CHECKBOX_JS = r"""() => {
  const groups = [
    ['turnstile', 'iframe[src*="challenges.cloudflare.com"], iframe[title*="Cloudflare security challenge"], ' +
                  'iframe[src*="challenge-platform"]'],
    ['recaptcha', 'iframe[src*="recaptcha"][src*="anchor"]'],
    ['hcaptcha', 'iframe[src*="hcaptcha.com"]'],
  ];
  const out = [], seen = new Set();
  for (const [kind, sel] of groups) {
    for (const f of document.querySelectorAll(sel)) {
      if (seen.has(f)) continue;
      seen.add(f);
      // reCAPTCHA's invisible mode parks a non-interactive badge iframe on the page: never click it
      const src = f.getAttribute('src') || '';
      if (src.includes('size=invisible') || src.includes('badge=')) continue;
      const r = f.getBoundingClientRect();
      if (r.width < 24 || r.height < 18) continue;
      if (r.right < 0 || r.bottom < 0 || r.left > innerWidth || r.top > innerHeight) continue;
      const cs = getComputedStyle(f);
      if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
      out.push({kind: kind, x: r.left, y: r.top, w: r.width, h: r.height});
    }
  }
  return out;
}"""

CF_LIKE_JS = r"""() => {
  const t = document.title || '';
  const h = document.documentElement ? document.documentElement.innerHTML.slice(0, 30000) : '';
  return /just a moment|checking your browser|verify you are human|请稍候|正在检查|确认您是真人|challenges\.cloudflare\.com|cf-turnstile|cf-chl/i.test(t + ' ' + h);
}"""


def _widget_hint(page):
    """Visible checkbox iframes as [{kind, x, y, w, h}] in CSS viewport coordinates."""
    try:
        hits = page.evaluate(CHECKBOX_JS) or []
    except Exception:
        hits = []
    return hits or _widget_hint_cdp(page)


def _cdp_box(cdp, node_id):
    try:
        q = cdp.send("DOM.getBoxModel", {"nodeId": node_id})["model"]["content"]
        xs, ys = q[0::2], q[1::2]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        if w < 24 or h < 18:
            return None
        return {"x": x, "y": y, "w": w, "h": h}
    except Exception:
        return None


def _widget_hint_cdp(page):
    """Walk the DOM through CDP with pierce=True.

    Cloudflare renders Turnstile inside a closed shadow root on many challenge pages, which page JS
    (and therefore CHECKBOX_JS) cannot see at all. CDP still exposes it, and DOM.getBoxModel gives
    its viewport box. This is the only way to find those widgets without guessing from a screenshot."""
    try:
        cdp = page.context.new_cdp_session(page)
    except Exception:
        return []
    try:
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        found = []
        for kind, node_id in widget_nodes(doc["root"]):
            box = _cdp_box(cdp, node_id)
            if box:
                found.append({"kind": kind, **box})
        return found
    except Exception:
        return []
    finally:
        try:
            cdp.detach()
        except Exception:
            pass


def widget_nodes(root):
    """[(kind, nodeId)] of the checkbox iframes in a DOM.getDocument(pierce=True) tree, page order.

    Shared by the Playwright path above and litbrowser's hands-off path, which reads the same tree over a bare
    DevTools connection. reCAPTCHA's invisible badge is never one of them."""
    found = []

    def walk(node, depth=0):
        if depth > 40:
            return
        if node.get("nodeName") == "IFRAME":
            attrs = node.get("attributes") or []
            attrs = {attrs[i]: attrs[i + 1] for i in range(0, len(attrs) - 1, 2)}
            src = (attrs.get("src") or "").lower()
            title = (attrs.get("title") or "").lower()
            kind = None
            if "challenges.cloudflare.com" in src or "challenge-platform" in src or "cloudflare" in title:
                kind = "turnstile"
            elif "recaptcha" in src or "recaptcha" in title:
                kind = "recaptcha"
            elif "hcaptcha" in src or "hcaptcha" in title:
                kind = "hcaptcha"
            if kind and "size=invisible" not in src and "badge=" not in src:
                found.append((kind, node.get("nodeId")))
        for key in ("children", "shadowRoots", "contentDocument", "pseudoElements", "templateContent"):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child, depth + 1)
            elif isinstance(child, list):
                for item in child:
                    walk(item, depth + 1)

    walk(root)
    return found


def _cf_like(page):
    try:
        return bool(page.evaluate(CF_LIKE_JS))
    except Exception:
        return False


def _is_clear(page, state_fn):
    if not state_fn:
        return False
    try:
        return not state_fn(page).get("challenge")
    except Exception:
        return False


def _first_look(page, state_fn, seconds=8):
    """Cloudflare often clears by itself, and its widget can load a second or two late.

    Returns 'clear' (no action needed), 'widget' (checkbox iframe is up), or 'other'."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        if _is_clear(page, state_fn):
            return "clear"
        if _widget_hint(page):
            return "widget"
        if not _cf_like(page):
            return "other"
        page.wait_for_timeout(1000)
    return "other"


def _os_mouse_enabled():
    return str(os.environ.get("LITDL_CAPTCHA_OS_MOUSE", "1")).strip().lower() not in ("0", "off", "false", "no")


def _os_click(page, x, y, log=None, keep=False):
    """Click through the Windows input queue (SetCursorPos + mouse_event), not CDP.

    Cloudflare can distinguish CDP-injected input from hardware input; this path produces the
    latter. `keep=True` leaves the pointer on the widget instead of restoring it - verification
    can take several seconds, and a cursor that teleports away mid-check looks wrong.
    Returns False when Windows input is unavailable so callers fall back to page.mouse."""
    if not _os_mouse_enabled():
        return False
    try:
        import osmouse
        if not osmouse.available():
            return False
        ok = osmouse.click(page, x, y, restore=not keep)
        if ok and log:
            log(f"       系统鼠标点击 ({x:.0f},{y:.0f})（SendInput 路径，光标留在控件上）")
        return ok
    except Exception as e:
        if log:
            log(f"       系统鼠标不可用：{str(e)[:70]}")
        return False


def _os_drag(page, x1, y1, x2, y2, duration=None):
    if not _os_mouse_enabled():
        return False
    try:
        import osmouse
        if not osmouse.available():
            return False
        return osmouse.drag(page, x1, y1, x2, y2, duration=duration or 0.8)
    except Exception:
        return False


def _click_like_human(page, x, y):
    """Approach from a nearby point, settle, then click - a teleporting cursor is a giveaway."""
    sx = max(2.0, x - random.randint(40, 150))
    sy = max(2.0, y - random.randint(20, 90))
    page.mouse.move(sx, sy)
    page.wait_for_timeout(random.randint(90, 170))
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(60, 140))
    page.mouse.click(x, y)


def _checkbox_point(kind, h):
    """Where the square actually sits inside a known widget.

    Turnstile's standard 300x65 card puts a 24 px box about 16 px from the left edge, slightly above
    the vertical centre; reCAPTCHA / hCaptcha anchors centre theirs a little further in. The measured
    SSRN challenge was (176.5, 280, 300x65) with the square centred near (193, 303)."""
    if kind == "turnstile" and h["w"] >= 240 and h["h"] <= 90:
        return h["x"] + max(14.0, min(22.0, h["w"] * 0.055)), h["y"] + h["h"] * 0.36
    return h["x"] + h["w"] * 0.095, h["y"] + h["h"] * 0.5


def _try_checkbox(page, state_fn, log):
    """Click the checkbox inside a known widget. True only when the challenge is gone afterwards."""
    hits = _widget_hint(page)
    if not hits:
        return None                                  # nothing known on screen
    h = hits[0]
    x, y = _checkbox_point(h["kind"], h)
    log(f"       识别到 {h['kind']} 复选框（{h['w']:.0f}×{h['h']:.0f}），点 ({x:.0f},{y:.0f})")
    if not _os_click(page, x, y, log=log, keep=True):
        _click_like_human(page, x, y)
    page.wait_for_timeout(random.randint(6000, 9000))
    if _clear_twice(page, state_fn, gap=1200):
        return True
    return False


def classify(page, cfg):
    """One screenshot -> one normalised plan (or None + error text)."""
    shot = _shot(page)
    body = _safe(lambda: page.inner_text("body")[:800], "")
    title = _safe(lambda: page.title()[:120], "")
    return plan_shot(shot, title, page.url, body, cfg)


def plan_shot(shot, title, url, body, cfg):
    """A screenshot record (see shot_from_bytes) -> one normalised plan in CSS pixels (or None + error text)."""
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": PLAN_USER.format(title=(title or "")[:120], url=(url or "")[:200],
                                                      w=shot["cw"], h=shot["ch"], body=(body or "")[:800])},
            {"type": "image_url", "image_url": {"url": f"data:{shot['mime']};base64,{shot['b64']}"}},
        ]},
    ]
    plan, err = _ai_json(messages, cfg)
    if plan is None:
        return None, err or "模型没有返回可解析的 JSON"
    return _normalise(plan, shot), None


# --------------------------------------------------------------------------- input
def drag(page, sx, sy, dx, dy, steps=28, duration=None):
    """Move the mouse from (sx, sy) by (dx, dy) in an eased quadratic arc, then release."""
    duration = duration or random.uniform(0.65, 0.95)
    tx, ty = sx + dx, sy + dy
    dist = math.hypot(dx, dy) or 1.0
    sign = 1 if random.random() > 0.5 else -1
    off = dist * random.uniform(0.10, 0.20) * sign
    cx = (sx + tx) / 2.0 - (dy / dist) * off
    cy = (sy + ty) / 2.0 + (dx / dist) * off
    page.mouse.move(sx, sy)
    page.wait_for_timeout(random.randint(120, 220))
    page.mouse.down()
    page.wait_for_timeout(random.randint(90, 180))
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)                    # ease-in-out: the knob starts and stops gently
        u = 1 - t
        x = u * u * sx + 2 * u * t * cx + t * t * tx
        y = u * u * sy + 2 * u * t * cy + t * t * ty
        page.mouse.move(x, y)
        page.wait_for_timeout(max(1, int(duration / steps * 1000)))
    page.wait_for_timeout(random.randint(60, 150))
    page.mouse.up()


def execute(page, plan, log=print):
    kind = plan.get("type")
    try:
        if kind == "slider":
            hx, hy = _center(plan["handle"])
            gap = _center(plan["gap"])
            piece = _center(plan["piece"]) if plan.get("piece") else None
            dx = gap[0] - (piece[0] if piece else hx)
            dy = 0.0 if abs(gap[1] - hy) > 40 else (gap[1] - hy)
            log(f"       拖拽 ({hx:.0f},{hy:.0f}) 移动 {dx:+.0f}px"
                + ("（按拼图块offset）" if piece else "（按把手到位）"))
            drag(page, hx, hy, dx, dy)
            return True
        if kind in ("click", "checkbox"):
            points = plan.get("points") or []
            for p in points:
                if kind == "checkbox" and _os_click(page, p[0], p[1], log=log, keep=True):
                    page.wait_for_timeout(random.randint(450, 900))
                    continue
                page.mouse.move(p[0], p[1])
                page.wait_for_timeout(random.randint(60, 140))
                page.mouse.click(p[0], p[1])
                page.wait_for_timeout(random.randint(450, 900))
            submit = plan.get("submit")
            if submit:
                page.wait_for_timeout(400)
                page.mouse.click(submit[0], submit[1])
            return bool(points or submit)
    except Exception as e:
        log(f"       操作失败：{str(e)[:100]}")
    return False


# --------------------------------------------------------------------------- judge
def _clear_twice(page, state_fn, gap=1500):
    """Hard signal from the caller's page_state: two clear looks in a row. None = no state_fn."""
    if not state_fn:
        return None
    try:
        if state_fn(page).get("challenge"):
            return False
        page.wait_for_timeout(gap)
        return not state_fn(page).get("challenge")
    except Exception:
        return None


def _ai_solved(page, cfg):
    shot = _shot(page)
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": VERIFY_USER},
            {"type": "image_url", "image_url": {"url": f"data:{shot['mime']};base64,{shot['b64']}"}},
        ]},
    ]
    verdict, err = _ai_json(messages, cfg, max_tokens=200)
    if not isinstance(verdict, dict):
        return None, err or "判定没返回 JSON"
    return bool(verdict.get("solved")), str(verdict.get("reason") or "")[:60]


# --------------------------------------------------------------------------- entry point
def solve(page, why="", *, state_fn=None, home=None, log=print):
    """Try to pass the verification widget currently showing on `page`. True = it is gone.

    The known-checkbox path (Cloudflare Turnstile / reCAPTCHA / hCaptcha) needs no model and runs
    first. The click-select and slider paths need a configured vision model; without one, solve()
    returns False after the checkbox attempt. `state_fn(page)` returns a dict with a `challenge` bool."""
    cfg = config(home)
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        if cfg["ready"]:
            log(f"CAPTCHA  {why[:40]}  AI 代过（{cfg['model']}，最多 {cfg['attempts']} 次）")
        else:
            log(f"CAPTCHA  {why[:40]}  未配模型：只尝试已知的验证复选框")
    except Exception:
        pass
    # Cloudflare often clears on its own after a few seconds, and a known checkbox iframe can be
    # clicked from its DOM rect - both beat asking a model to guess a 20 px box.
    try:
        look = _first_look(page, state_fn)
    except Exception:
        look = "other"
    if look == "clear":
        log("       验证页自己过了，继续")
        return True
    tried_checkbox = False
    if look == "widget":
        tried_checkbox = True
        try:
            hit = _try_checkbox(page, state_fn, log)
        except Exception as e:
            hit = None
            log(f"       复选框点击出错：{str(e)[:80]}")
        if hit:
            log("       验证已通过（复选框），继续")
            return True
        # the click may have opened an interactive challenge: let the vision pass handle it
    if not cfg["ready"]:                            # no model: checkbox was the only thing we could do
        return False
    for attempt in range(1, cfg["attempts"] + 1):
        try:
            page.wait_for_timeout(400)
        except Exception:
            return False
        plan, err = classify(page, cfg)
        if plan is None:
            log(f"       第{attempt}次：模型没给出可用计划（{str(err)[:80]}）")
            return False
        kind = plan.get("type")
        if kind == "none":
            log(f"       第{attempt}次：画面里没有可识别的验证控件"
                + (f"（{plan.get('note')[:50]}）" if plan.get("note") else ""))
            return False
        log(f"       第{attempt}次：{kind}"
            + (f"，依次点 {len(plan.get('points') or [])} 处" if kind != "slider" else "，拖滑块"))
        if kind == "checkbox":
            # never blind-click a checkbox the model "sees": require a real widget iframe, and never
            # click the same one twice in one solve (Cloudflare treats rapid repeats as a failure)
            if tried_checkbox:
                log("       复选框已经点过，不再连点，交给用户")
                return False
            hits = _widget_hint(page)
            if not hits:
                log("       模型说是复选框，但页面上没有已知的验证 iframe，不盲点")
                return False
            h = hits[0]
            plan = {"type": "checkbox", "submit": None, "points": [list(_checkbox_point(h["kind"], h))]}
            tried_checkbox = True
        if not execute(page, plan, log=log):
            return False
        try:
            page.wait_for_timeout(random.randint(1200, 2000))
        except Exception:
            pass
        clear = _clear_twice(page, state_fn)
        if clear:
            log("       验证已通过（页面已无验证控件），继续")
            return True
        verdict, reason = _ai_solved(page, cfg)
        if verdict:
            log(f"       模型判定已通过{'：' + reason if reason else ''}，继续")
            return True
        log(f"       第{attempt}次没过" + (f"：{reason}" if reason else ""))
        try:
            page.wait_for_timeout(random.randint(900, 1800))
        except Exception:
            pass
    return False


def describe(home=None):
    """One line for `litbrowser status`."""
    cfg = config(home)
    if not cfg["ready"]:
        return "AI 过验证：只点已知复选框（未配模型；点选/滑块不可用）"
    return f"AI 过验证：开（{cfg['model']}，最多 {cfg['attempts']} 次，失败交给用户）"


if __name__ == "__main__":
    print(describe())
