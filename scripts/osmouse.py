#!/usr/bin/env python3
"""Windows-level mouse input for the dedicated Chrome window.

`page.mouse` goes through Playwright -> CDP Input.dispatchMouseEvent. Those events are trusted,
but Cloudflare's managed challenge can still tell them apart from hardware input. This module
moves the real cursor with SetCursorPos and clicks with mouse_event, so the browser receives the
same events a physical mouse produces.

Page coordinates are CSS pixels. The mapping to physical screen pixels is measured at click time:

    scale   = client_width / innerWidth          (captures display scaling and page zoom)
    origin  = ClientToScreen(hwnd, 0,0)
    ui_h    = client_height - innerHeight*scale  (tab strip + toolbar drawn inside the client area)
    screen  = (origin.x + x*scale, origin.y + ui_h + y*scale)

Everything fails closed: if the window cannot be found, focused, or the viewport reports zero,
callers get False and fall back to Playwright's mouse.

Only Windows. `LITDL_CAPTCHA_OS_MOUSE=0` turns the caller-side use off.
"""
import ctypes
import math
import os
import random
import time
from ctypes import wintypes

IS_WIN = os.name == "nt"

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SW_RESTORE = 9


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


if IS_WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:                                             # physical pixels everywhere, not virtualised
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def available():
    return IS_WIN


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _chrome_windows():
    found = []

    def cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd) and _class_name(hwnd) == "Chrome_WidgetWin_1":
            found.append(hwnd)
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return found


def _window_pid(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _browser_pid(page):
    """PID of the Chrome browser process behind this page, via CDP SystemInfo."""
    try:
        session = page.context.browser.new_browser_cdp_session()
        try:
            info = session.send("SystemInfo.getProcessInfo").get("processInfo", [])
        finally:
            session.detach()
        for proc in info:
            if proc.get("type") == "browser":
                return int(proc.get("id"))
    except Exception:
        pass
    return None


def _pick_window(page):
    """The dedicated Chrome window only.

    The user's everyday Chrome uses the same window class. Never fall back to "any Chrome window
    in front" - we match the CDP browser process PID first, then a title from one of our own tabs.
    Returning None makes the caller fall back to Playwright's CDP mouse, which cannot hit the
    wrong application."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    page.wait_for_timeout(350)

    pid = _browser_pid(page)
    windows = _chrome_windows()
    if pid:
        windows = [h for h in windows if _window_pid(h) == pid]
        if not windows:
            return None
    if not windows:
        return None

    titles = []
    try:
        for p in page.context.pages:
            try:
                t = (p.title() or "").strip()
                if t:
                    titles.append(t[:24])
            except Exception:
                pass
    except Exception:
        pass

    match = None
    for hwnd in windows:
        text = _window_text(hwnd)
        if any(t and t in text for t in titles):
            match = hwnd
            break
    if match is None:
        match = windows[0] if len(windows) == 1 else None
    if match is None:
        return None
    return match if _focus(match) else None


VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
GA_ROOTOWNER = 3


def _in_front(hwnd):
    """The window is in front: it is the foreground window, or the foreground window is one of its own pop-ups.
    Chrome opens small top-level windows of its own (the "翻译此页？" bubble on a Chinese page, 2026-11-22); while
    one has the focus the browser window is still the one on top, and a click on the page lands there."""
    fg = _user32.GetForegroundWindow()
    if not fg:
        return False
    if fg == hwnd:
        return True
    try:
        return _user32.GetAncestor(fg, GA_ROOTOWNER) == hwnd
    except Exception:
        return False


def _focus(hwnd):
    """Bring the window to the foreground, or return False.

    Windows' foreground lock rejects SetForegroundWindow from a background process (a fetch started
    from the terminal is one). The standard unlocks are a synthetic Alt keypress and
    AttachThreadInput against the current foreground thread; both are tried, and the result is
    verified rather than assumed."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    if _in_front(hwnd):
        return True
    try:
        _user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.15)
    if _in_front(hwnd):
        return True
    try:                                             # Alt press/release unlocks the foreground lock
        _user32.keybd_event(VK_MENU, 0, 0, 0)
        _user32.SetForegroundWindow(hwnd)
        _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    time.sleep(0.20)
    if _in_front(hwnd):
        return True
    try:                                             # attach to the foreground thread, then set
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None)
        my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        if fg_tid and fg_tid != my_tid:
            _user32.AttachThreadInput(my_tid, fg_tid, True)
            try:
                _user32.SetForegroundWindow(hwnd)
            finally:
                _user32.AttachThreadInput(my_tid, fg_tid, False)
    except Exception:
        pass
    time.sleep(0.20)
    return _in_front(hwnd)


def css_to_screen(client_x, client_y, client_w, client_h, view_w, view_h, x, y):
    """A CSS point of the page -> physical screen pixel. Pure arithmetic, see the module docstring."""
    if view_w <= 0 or view_h <= 0:
        raise RuntimeError("页面视口为 0")
    scale = client_w / view_w
    ui_h = client_h - view_h * scale                 # tabs + toolbar live inside the client area
    return int(round(client_x + x * scale)), int(round(client_y + ui_h + y * scale))


def _client(hwnd):
    pt = _POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        raise RuntimeError("ClientToScreen 失败")
    cr = _RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(cr)):
        raise RuntimeError("GetClientRect 失败")
    return pt.x, pt.y, cr.right, cr.bottom


def _origin(page, hwnd):
    cx, cy, client_w, client_h = _client(hwnd)
    vp = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
    page_w, page_h = int(vp.get("w") or 0), int(vp.get("h") or 0)
    if page_w <= 0 or page_h <= 0:
        raise RuntimeError("页面视口为 0")
    scale = client_w / page_w
    ui_h = client_h - page_h * scale                 # tabs + toolbar live inside the client area
    return cx, cy + ui_h, scale


def _to_screen(page, hwnd, x, y):
    ox, oy, scale = _origin(page, hwnd)
    return int(round(ox + x * scale)), int(round(oy + y * scale))


def _cursor():
    p = _POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _restore_if_still(old, x, y):
    now = _cursor()
    if abs(now[0] - x) <= 2 and abs(now[1] - y) <= 2:
        _user32.SetCursorPos(old[0], old[1])


def click(page, x, y, restore=True):
    """Click page CSS point (x, y) with the OS cursor. True if the click was dispatched."""
    if not IS_WIN:
        return False
    hwnd = _pick_window(page)
    if not hwnd or not _focus(hwnd):
        return False
    sx, sy = _to_screen(page, hwnd, x, y)
    old = _cursor()
    _user32.SetCursorPos(sx, sy)
    time.sleep(random.uniform(0.09, 0.18))
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.05, 0.12))
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(random.uniform(0.55, 0.90))
    if restore:
        _restore_if_still(old, sx, sy)
    return True


def drag(page, x1, y1, x2, y2, duration=0.8, restore=True):
    """Press at (x1,y1), follow an eased arc to (x2,y2), release. True if dispatched."""
    if not IS_WIN:
        return False
    hwnd = _pick_window(page)
    if not hwnd or not _focus(hwnd):
        return False
    sx1, sy1 = _to_screen(page, hwnd, x1, y1)
    sx2, sy2 = _to_screen(page, hwnd, x2, y2)
    old = _cursor()
    _user32.SetCursorPos(sx1, sy1)
    time.sleep(random.uniform(0.09, 0.17))
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.09, 0.16))
    dx, dy = sx2 - sx1, sy2 - sy1
    dist = math.hypot(dx, dy) or 1.0
    offset = dist * random.uniform(0.10, 0.18) * (1 if random.random() > 0.5 else -1)
    cx = (sx1 + sx2) / 2 - dy / dist * offset
    cy = (sy1 + sy2) / 2 + dx / dist * offset
    steps = 30
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        u = 1 - t
        x = u * u * sx1 + 2 * u * t * cx + t * t * sx2
        y = u * u * sy1 + 2 * u * t * cy + t * t * sy2
        _user32.SetCursorPos(int(round(x)), int(round(y)))
        time.sleep(max(0.002, duration / steps))
    time.sleep(random.uniform(0.05, 0.12))
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(random.uniform(0.55, 0.90))
    if restore:
        _restore_if_still(old, sx2, sy2)
    return True


# --------------------------------------------------------------------------- without a Playwright page
# litbrowser's hands-off path lets go of the browser before a verification page loads, so there is no page
# object to ask. It knows the browser's PID, the tab's title and the viewport size (read over a bare DevTools
# connection); that is enough to find the window and map the point. Every verification goes this way since
# 2026-11-22 (user: "如果遇到验证，都用这个方式过，就是松开脚本再过").

def window_for(pid, title=""):
    """The dedicated Chrome's top-level window showing `title`, or None. Never another application's window."""
    if not IS_WIN or not pid:
        return None
    windows = [h for h in _chrome_windows() if _window_pid(h) == pid]
    if title:
        # a window whose title is not this tab's shows another tab: a click there would land on the wrong page
        hit = [h for h in windows if title[:24] in _window_text(h)]
        return hit[0] if hit else None
    return windows[0] if len(windows) == 1 else None


def focus(hwnd):
    return bool(IS_WIN and hwnd and _focus(hwnd))


def point_is_on(hwnd, sx, sy):
    """Is screen pixel (sx, sy) showing `hwnd` (or one of its own pop-ups), i.e. not covered by another window?"""
    try:
        w = _user32.WindowFromPoint(_POINT(sx, sy))
        return bool(w) and _user32.GetAncestor(w, GA_ROOTOWNER) == hwnd
    except Exception:
        return False


def foreground_text():
    try:
        return _window_text(_user32.GetForegroundWindow())
    except Exception:
        return ""


def _screen_point(hwnd, view_w, view_h, x, y):
    """(sx, sy) of CSS point (x, y), or None when the point may not be clicked.

    Windows refuses a background process the foreground while the user is busy elsewhere. Clicking anyway is safe
    only when the point itself shows this window: the click lands there and brings it forward."""
    focused = _focus(hwnd)
    cx, cy, client_w, client_h = _client(hwnd)
    sx, sy = css_to_screen(cx, cy, client_w, client_h, view_w, view_h, x, y)
    if not focused and not point_is_on(hwnd, sx, sy):
        return None
    return sx, sy


def drag_css(hwnd, view_w, view_h, x1, y1, x2, y2, duration=0.8, restore=False):
    """Press at CSS point (x1, y1) of the tab showing in `hwnd`, follow an eased arc to (x2, y2), release."""
    if not IS_WIN or not hwnd:
        return False
    a = _screen_point(hwnd, view_w, view_h, x1, y1)
    if not a:
        return False
    cx, cy, client_w, client_h = _client(hwnd)
    b = css_to_screen(cx, cy, client_w, client_h, view_w, view_h, x2, y2)
    if not point_is_on(hwnd, b[0], b[1]) and not _in_front(hwnd):
        return False
    return _drag_screen(a, b, duration, restore)


def _drag_screen(a, b, duration, restore):
    (sx1, sy1), (sx2, sy2) = a, b
    old = _cursor()
    _user32.SetCursorPos(sx1, sy1)
    time.sleep(random.uniform(0.12, 0.25))
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.09, 0.16))
    dx, dy = sx2 - sx1, sy2 - sy1
    dist = math.hypot(dx, dy) or 1.0
    offset = dist * random.uniform(0.10, 0.18) * (1 if random.random() > 0.5 else -1)
    cx = (sx1 + sx2) / 2 - dy / dist * offset
    cy = (sy1 + sy2) / 2 + dx / dist * offset
    steps = 30
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        u = 1 - t
        _user32.SetCursorPos(int(round(u * u * sx1 + 2 * u * t * cx + t * t * sx2)),
                             int(round(u * u * sy1 + 2 * u * t * cy + t * t * sy2)))
        time.sleep(max(0.002, duration / steps))
    time.sleep(random.uniform(0.05, 0.12))
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(random.uniform(0.55, 0.90))
    if restore:
        _restore_if_still(old, sx2, sy2)
    return True


def hover_css(hwnd, view_w, view_h, x, y):
    """Move the cursor onto CSS point (x, y) and leave it there (some sliders show their puzzle on hover)."""
    if not IS_WIN or not hwnd:
        return False
    p = _screen_point(hwnd, view_w, view_h, x, y)
    if not p:
        return False
    _user32.SetCursorPos(p[0], p[1])
    return True


def click_css(hwnd, view_w, view_h, x, y, restore=False):
    """Click CSS point (x, y) of the tab showing in `hwnd`, whose viewport is view_w x view_h CSS pixels."""
    if not IS_WIN or not hwnd:
        return False
    p = _screen_point(hwnd, view_w, view_h, x, y)
    if not p:
        return False
    sx, sy = p
    old = _cursor()
    _user32.SetCursorPos(sx, sy)
    time.sleep(random.uniform(0.25, 0.45))
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.06, 0.12))
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(random.uniform(0.55, 0.90))
    if restore:
        _restore_if_still(old, sx, sy)
    return True


if __name__ == "__main__":
    print("osmouse available:", available())
