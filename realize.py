"""
"""
import sys
import ctypes
import time
import datetime
import socket
import subprocess
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import ntplib
import win32api
import win32process
import psutil
import requests

import json
import re

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

# ====== Chrome 用設定 (必要に応じてパス変更) ======
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROFILE_BASE = r"C:\ChromeProfiles"
PROFILE = PROFILE_BASE + r"\SeleniumProfileChrome"
PORT = 9222

EVENT_URL = "https://ticketdive.com/event/numa2026"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ====== 基本タイミング ======
PRELOAD_EVENT_ENABLED = True
PRELOAD_EVENT_AT = "19:59:20.000"
EVENT_AT = "20:00:00.000"

FAST_POLL = 0.004
POLL_SHORT = 0.016

URL_CHANGE_TIMEOUT = 40.0

PRACTICE_MODE = False
EVENT_SHIFT_REAL_MS = 0

# ====== CVV 関連 ======
CVV_CODE = "985"  # テスト用（あなたが自動入力している値）
CVV_IFRAME_SELECTOR = "#card-cvc iframe"
CVV_INPUT_SELECTOR = "input[name='cvv']"
CVV_APPEAR_TIMEOUT = 40.0

CVV_CACHE_KEY = "td_cvv_iframe_index_v2"
CVV_METHOD_MARKER_KEY = "td_cvv_last_method"
# payment 関連URL検出用
PAYMENT_PATTERNS = [
    r"cardinalcommerce", r"paysec", r"3dssvgw", r"rakuten", r"cafis", r"token",
    r"/tokens?", r"/card_token", r"/createToken", r"/payments?", r"tokenize"
]
_payment_re = re.compile("|".join(PAYMENT_PATTERNS), re.I)

# 検出タイムアウト（ミリ秒級のトレードオフ）
QUICK_DETECT_TIMEOUT = 0.12
NETWORK_DETECT_TIMEOUT = 0.6
SUBMIT_ENABLED_TIMEOUT = 0.18
TOTAL_DETECTION_TIMEOUT = 3.0  # 最終フォールバック前の最大待機（必要なら延長）

# ====== グローバル ======
driver = None
fire_time = None
reload_start_dt = None
TD_OFFSET_SEC = None
EVENT_FIRE_LOCAL_DT = None

# ====== ユーティリティ ======
def now_ms():
    return time.time()

def ts_human():
    return datetime.datetime.now().isoformat(sep=' ', timespec='milliseconds')

def log(s):
    print(s, flush=True)


# ====== OS / プロセス優先度 ======
def boost_timer_resolution_1ms():
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

def boost_process_priority_high():
    try:
        win32process.SetPriorityClass(
            win32api.GetCurrentProcess(),
            win32process.HIGH_PRIORITY_CLASS
        )
    except Exception:
        pass

def boost_browser_priority_high(d):
    try:
        psutil.Process(d.service.process.pid).nice(psutil.HIGH_PRIORITY_CLASS)
    except Exception:
        pass

def relaunch_as_admin():
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )
    sys.exit()


# ====== 時刻同期 & TD offset (同様の実装) ======
def sync_os_time_now():
    c = ntplib.NTPClient()
    last_err = None
    for host in ["ntp.nict.jp", "time.google.com", "time.windows.com"]:
        try:
            r = c.request(host, version=3, timeout=1.5)
            ntp_utc = datetime.datetime.fromtimestamp(r.tx_time, tz=datetime.timezone.utc)
            win32api.SetSystemTime(
                ntp_utc.year,
                ntp_utc.month,
                ntp_utc.weekday(),
                ntp_utc.day,
                ntp_utc.hour,
                ntp_utc.minute,
                ntp_utc.second,
                int(ntp_utc.microsecond / 1000)
            )
            print(f"[NTP] OS synced to {host}", flush=True)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError(f"NTP sync failed: {last_err}")


def get_ticketdive_base_url():
    try:
        p = urlparse(EVENT_URL)
        if not p.scheme or not p.netloc:
            return "https://ticketdive.com/"
        return f"{p.scheme}://{p.netloc}/"
    except Exception:
        return "https://ticketdive.com/"


def measure_ticketdive_offset(samples=7, connect_timeout=2.0, read_timeout=2.0, max_rtt=1.5, allow_zero_fallback=True):
    offsets = []
    base_url = get_ticketdive_base_url()
    urls = [base_url]
    if EVENT_URL:
        urls.append(EVENT_URL)

    sess = requests.Session()
    print(f"[TD_OFFSET] measure from {', '.join(urls)}", flush=True)

    for i in range(samples):
        for url in urls:
            t0 = time.time()
            try:
                try:
                    r = sess.head(url, headers=DEFAULT_HEADERS, timeout=(connect_timeout, read_timeout), allow_redirects=True)
                    if r.status_code >= 400:
                        raise RuntimeError(f"HEAD status={r.status_code}")
                except Exception:
                    r = sess.get(url, headers=DEFAULT_HEADERS, timeout=(connect_timeout, read_timeout), stream=True)
                t1 = time.time()
            except Exception as e:
                print(f"[TD_OFFSET] req error {i+1}/{samples} url={url}: {e}", flush=True)
                continue

            if "Date" not in r.headers:
                print(f"[TD_OFFSET] no Date header {i+1}/{samples} url={url} (status={getattr(r,'status_code','???')})", flush=True)
                continue

            try:
                server_dt = parsedate_to_datetime(r.headers["Date"])
                server_ts = server_dt.timestamp()
            except Exception as e:
                print(f"[TD_OFFSET] Date parse error {i+1}/{samples} url={url}: {e}", flush=True)
                continue

            rtt = t1 - t0
            mid_local = (t0 + t1) / 2.0
            offset = server_ts - mid_local

            offsets.append((offset, rtt))
            print(f"[TD_OFFSET] sample {i+1}/{samples}: url={url} offset={offset:+.3f}s rtt={rtt*1000:.1f}ms", flush=True)
            break

        time.sleep(POLL_SHORT)

    if not offsets:
        msg = "TicketDive offset measurement failed (no valid samples)"
        if allow_zero_fallback:
            print(f"[TD_OFFSET] {msg} → use 0.000s (fallback: NTP only)", flush=True)
            return 0.0
        raise RuntimeError(msg)

    good_offsets = [o for (o, rtt) in offsets if rtt <= max_rtt]
    if not good_offsets:
        print("[TD_OFFSET] all RTTs too large, using all samples", flush=True)
        good_offsets = [o for (o, rtt) in offsets]

    good_offsets.sort()
    median = good_offsets[len(good_offsets)//2]
    return median


# ====== Chrome attach/driver init ======
def launch_chrome_debug(profile, port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.2)
        return True
    except Exception:
        subprocess.Popen([
            CHROME_EXE,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def init_driver(profile, port):
    if not launch_chrome_debug(profile, port):
        raise RuntimeError("Chrome attach failed")
    o = ChromeOptions()
    o.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    caps = DesiredCapabilities.CHROME.copy()
    caps['goog:loggingPrefs'] = {'performance': 'ALL'}
    try:
        d = webdriver.Chrome(options=o, desired_capabilities=caps)
    except Exception:
        d = webdriver.Chrome(options=o)
    return d


def is_driver_alive(d):
    try:
        if d is None:
            return False
        _ = d.current_url
        return True
    except Exception:
        return False


def restart_driver(reason=""):
    """
    Chrome attach セッション切れ対策。
    invalid session id / renderer disconnected が出た場合に driver だけ再attachする。
    """
    global driver
    try:
        if reason:
            print(f"[RECOVER] driver reconnect start ({reason})", flush=True)
        else:
            print("[RECOVER] driver reconnect start", flush=True)
    except Exception:
        pass

    try:
        driver = init_driver(PROFILE, PORT)
        boost_browser_priority_high(driver)
        install_early_js(driver)
        install_cvv_postmessage_listener(driver)
        print("[RECOVER] driver reconnect OK", flush=True)
        return True
    except Exception as e:
        print(f"[RECOVER] driver reconnect FAILED: {e}", flush=True)
        return False


def safe_get(url, retries=3, sleep_sec=0.25):
    """
    driver.get() 中にChrome/rendererとの接続が切れた場合、
    再attachして同じURLを再取得する。
    """
    global driver
    last_err = None

    for i in range(retries + 1):
        try:
            if not is_driver_alive(driver):
                if not restart_driver("before get"):
                    time.sleep(sleep_sec)
                    continue

            driver.get(url)
            return True

        except Exception as e:
            last_err = e
            msg = str(e)
            low = msg.lower()
            print(f"[RECOVER] driver.get failed {i+1}/{retries+1}: {msg}", flush=True)

            if (
                "invalid session id" in low
                or "session deleted" in low
                or "disconnected" in low
                or "unable to receive message from renderer" in low
                or "browser has closed" in low
                or "not connected to devtools" in low
            ):
                restart_driver("session lost during get")
                time.sleep(sleep_sec)
                continue

            time.sleep(sleep_sec)

    print(f"[RECOVER] driver.get final FAILED: {last_err}", flush=True)
    return False


# ====== イベントカード周り ======
def click_top_event_card(d, timeout=2.0):
    sel = "img.EventItemImage_image__4jAs1"
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            cards = [e for e in d.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
        except Exception:
            return False
        if cards:
            try:
                cards.sort(key=lambda x: x.location.get("y", 999999))
            except Exception:
                pass
            el = cards[0]
            try:
                el.click()
            except Exception:
                try:
                    d.execute_script("arguments[0].click();", el)
                except Exception:
                    return False
            return True
        time.sleep(FAST_POLL)
    return False


def cache_favorite_top_event_card_js(d, timeout=4.0):
    """
    favoriteページ上で一番上のイベントカードをJS側に保持する。
    発火時は Selenium の find_elements/click を避け、JSで即クリックするためのキャッシュ。
    """
    script = r"""
        try {
            const imgs = Array.from(document.querySelectorAll('img.EventItemImage_image__4jAs1'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (ar.top - br.top) || (ar.left - br.left);
                });

            if (!imgs.length) {
                window.__TD_FAV_CARD_EL = null;
                window.__TD_FAV_CARD_CACHED_AT = Date.now();
                return {ok:false, reason:'no visible favorite card'};
            }

            const img = imgs[0];
            const clickable = img.closest('a,button,[role="button"],div') || img;
            window.__TD_FAV_CARD_EL = clickable;
            window.__TD_FAV_CARD_IMG = img;
            window.__TD_FAV_CARD_CACHED_AT = Date.now();

            const r = clickable.getBoundingClientRect();
            return {
                ok: true,
                tag: clickable.tagName,
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top + r.height / 2),
                href: clickable.href || '',
                text: (clickable.innerText || img.alt || '').slice(0, 80)
            };
        } catch(e) {
            window.__TD_FAV_CARD_EL = null;
            return {ok:false, reason:String(e)};
        }
    """

    end = time.perf_counter() + timeout
    last = None
    while time.perf_counter() < end:
        try:
            last = d.execute_script(script)
            if last and last.get("ok"):
                print("[PRELOAD] favorite top event card JS cached", flush=True)
                return True
        except Exception as e:
            last = {"ok": False, "reason": str(e)}
        time.sleep(FAST_POLL)

    print(f"[PRELOAD] favorite top event card JS cache failed: {last}", flush=True)
    return False


def click_cached_favorite_card_js(d, timeout=1.0):
    """
    キャッシュ済みfavoriteカードをJSで即クリックする。
    キャッシュが死んでいた場合だけ、その場で一番上カードを再探索してクリックする。
    """
    script = r"""
        try {
            function visible(el) {
                if (!el || !el.isConnected) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }
            function topCard() {
                const imgs = Array.from(document.querySelectorAll('img.EventItemImage_image__4jAs1'))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.top - br.top) || (ar.left - br.left);
                    });
                if (!imgs.length) return null;
                return imgs[0].closest('a,button,[role="button"],div') || imgs[0];
            }

            let el = window.__TD_FAV_CARD_EL;
            let usedCache = true;

            if (!visible(el)) {
                el = topCard();
                window.__TD_FAV_CARD_EL = el;
                usedCache = false;
            }

            if (!visible(el)) {
                return {ok:false, reason:'no cached/refound visible card'};
            }

            try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}

            const r = el.getBoundingClientRect();
            const x = Math.round(r.left + r.width / 2);
            const y = Math.round(r.top + r.height / 2);

            // React系のonClickに乗せるため、pointer/mouseイベントを一通り送る
            const opts = {bubbles:true, cancelable:true, view:window, clientX:x, clientY:y};
            try { el.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch(e) {}
            try { el.dispatchEvent(new MouseEvent('mousedown', opts)); } catch(e) {}
            try { el.dispatchEvent(new PointerEvent('pointerup', opts)); } catch(e) {}
            try { el.dispatchEvent(new MouseEvent('mouseup', opts)); } catch(e) {}
            try { el.click(); } catch(e) {}

            return {ok:true, usedCache, x, y, tag:el.tagName};
        } catch(e) {
            return {ok:false, reason:String(e)};
        }
    """

    end = time.perf_counter() + timeout
    last = None
    while time.perf_counter() < end:
        try:
            last = d.execute_script(script)
            if last and last.get("ok"):
                return True
        except Exception as e:
            last = {"ok": False, "reason": str(e)}
        time.sleep(FAST_POLL)

    # 失敗理由を必ずログに残す。本番でJSキャッシュが効いたか追えるようにする。
    print(f"[JSCLICK] favorite cached card JS click failed: {last}", flush=True)
    return False

def wait_for_quantity_select_after_card_click(d, timeout=2.5):
    """
    発火時のfavoriteカードクリック後、ページ切り替わり後に枚数selectが出るまで待つ。
    ここで確認してから EARLY_JS を reset/fire することで、
    SPA遷移前にEARLY_JSが走って枚数selectを取りこぼすのを防ぐ。
    """
    start = time.perf_counter()
    end = start + timeout
    last_url = ""

    script = r"""
        try {
            const q = document.evaluate(
                "//select[contains(@class,'TicketTypeCard_numberSelector__')]",
                document, null, 9, null
            ).singleNodeValue;
            if (!q) {
                return {ok:false, url:location.href, reason:'no quantity select'};
            }
            const r = q.getBoundingClientRect();
            const visible = r.width > 0 && r.height > 0;
            return {ok:visible, url:location.href, reason:visible ? 'quantity select visible' : 'quantity select hidden'};
        } catch(e) {
            return {ok:false, url:location.href, reason:String(e)};
        }
    """

    while time.perf_counter() < end:
        try:
            result = d.execute_script(script)
            if result:
                last_url = result.get("url") or last_url
                if result.get("ok"):
                    dt = time.perf_counter() - start
                    log(f"[FLOW] quantity_select_seen before EARLY_JS +{dt:.3f}s")
                    return True
        except Exception:
            pass
        time.sleep(FAST_POLL)

    dt = time.perf_counter() - start
    log(f"[FLOW] quantity_select_wait timeout before EARLY_JS +{dt:.3f}s url={last_url}")
    return False



def wait_and_click_top_stage_select(d, timeout=4.0):
    xpath = "//button[contains(@class,'StageListItem_active__') and .//span[normalize-space(text())='選択する']]"
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            btns = d.find_elements(By.XPATH, xpath)
        except Exception:
            return False
        visible = [b for b in btns if b.is_displayed()]
        if visible:
            btn = visible[0]
            try:
                btn.click()
            except Exception:
                try:
                    d.execute_script("arguments[0].click();", btn)
                except Exception:
                    return False
            print("[STAGE] 一番上の「選択する」を即クリック", flush=True)
            return True
        time.sleep(FAST_POLL)
    return False


# ====== EARLY JS (同様) ======
EARLY_JS = r"""
(function(){
 if(window._tdInstalled)return;
 window._tdInstalled=true;
 window.__TD_FIRE=false;
 window.__TD_STOP=false;
 window.__TD_QUANTITY_DONE=false;
 window.__TD_APPLY_DONE=false;
 const Q="//select[contains(@class,'TicketTypeCard_numberSelector__')]";
 const B="//button[.//span[normalize-space(.)='申し込みをする']]";
 const O="//select[contains(@class,'Select_select__')]";
 function xp(x){ try{ return document.evaluate(x,document,null,9,null).singleNodeValue; }catch(e){ return null; } }
 function setMaxOnce(s){ if(!s||!s.options||!s.options.length)return false; var o=null; for(var i=s.options.length-1;i>=0;i--){ if(!s.options[i].disabled){ o=s.options[i]; break; } } if(!o)return false; if(s.value===o.value)return false; s.value=o.value; s.dispatchEvent(new Event('input',{bubbles:true})); s.dispatchEvent(new Event('change',{bubbles:true})); return true; }
 function setMax(s){ if(!s||!s.options||!s.options.length)return; var o=null; for(var i=s.options.length-1;i>=0;i--){ if(!s.options[i].disabled){ o=s.options[i]; break; } } if(!o)return; s.value=o.value; s.dispatchEvent(new Event('input',{bubbles:true})); s.dispatchEvent(new Event('change',{bubbles:true})); }
 function tick(){
  if(window.__TD_STOP)return;
  if(!window.__TD_FIRE)return;
  if(window.__TD_QUANTITY_DONE===false){
    var q=xp(Q);
    if(q){
      if(setMaxOnce(q)){
        window.__TD_QUANTITY_DONE=true;
      }
    }
  }
  if(window.__TD_APPLY_DONE===false){
    var b=xp(B);
    if(b && !b.disabled && b.getAttribute('aria-disabled')!=='true'){
      window.__TD_APPLY_DONE=true;
      requestAnimationFrame(function(){b.click();});
    }
  }
  var o=xp(O);
  if(o){ setMax(o); }
 }
 try{ window.__TD_OB=new MutationObserver(tick); window.__TD_OB.observe(document.body||document,{childList:true,subtree:true}); }catch(e){}
 try{ if(!window.__TD_TICK_TIMER){ window.__TD_TICK_TIMER=setInterval(tick,50); } }catch(e){}
 window.__TD_FORCE_TICK=tick;
})();
"""

def install_early_js(d):
    try:
        d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": EARLY_JS})
    except Exception:
        pass


# ====== CVV 書込 (JS-only) & キャッシュ対応 (同等) ======
def fill_cvv_iframe(d, code=CVV_CODE, appear_timeout=CVV_APPEAR_TIMEOUT):
    print("[CVV] (JS-only) iframe を待機中 (キャッシュ対応)...", flush=True)
    start = time.perf_counter()
    try:
        d.execute_script(f"try{{ window.localStorage.setItem('{CVV_METHOD_MARKER_KEY}', 'none'); }}catch(e){{}}")
    except Exception:
        pass

    try:
        cached = None
        try:
            cached = d.execute_script(f"try{{return window.localStorage.getItem('{CVV_CACHE_KEY}');}}catch(e){{return null;}}")
        except Exception:
            cached = None

        if cached is not None:
            try:
                idx = int(cached)
            except Exception:
                idx = None

            if idx is not None:
                try:
                    iframes = d.find_elements(By.CSS_SELECTOR, CVV_IFRAME_SELECTOR)
                except Exception:
                    iframes = []

                if 0 <= idx < len(iframes):
                    iframe = iframes[idx]
                    if iframe.is_displayed():
                        try:
                            d.switch_to.frame(iframe)
                            ok = d.execute_script(
                                "try{ var inp = document.querySelector(arguments[0]); if(!inp) return false; inp.focus && inp.focus(); inp.value = arguments[1]; inp.dispatchEvent(new Event('input',{bubbles:true})); return true; }catch(e){return false;}",
                                CVV_INPUT_SELECTOR, str(code)
                            )
                            try:
                                d.switch_to.default_content()
                            except Exception:
                                pass
                            if ok:
                                try:
                                    d.execute_script(f"try{{ window.localStorage.setItem('{CVV_METHOD_MARKER_KEY}','js'); }}catch(e){{}}")
                                except Exception:
                                    pass
                                print(f"[CVV] cached index {idx} で JS 入力成功", flush=True)
                                try:
                                    d.execute_script(f"try{{ window.localStorage.setItem('{CVV_CACHE_KEY}', arguments[0]); }}catch(e){{}}", str(idx))
                                except Exception:
                                    pass
                                return True
                            else:
                                print(f"[CVV] cached index {idx} で JS 書込失敗 → 再探索へ", flush=True)
                        except Exception as e:
                            try:
                                d.switch_to.default_content()
                            except Exception:
                                pass
                            print(f"[CVV] cached index {idx} で例外: {e} → 再探索", flush=True)
    except Exception as e:
        print(f"[CVV] キャッシュ読み取り例外: {e} (無視)", flush=True)

    while time.perf_counter() - start < appear_timeout:
        try:
            iframes = d.find_elements(By.CSS_SELECTOR, CVV_IFRAME_SELECTOR)
        except Exception:
            time.sleep(POLL_SHORT)
            continue

        for idx, iframe in enumerate(iframes):
            if not iframe.is_displayed():
                continue
            try:
                d.switch_to.frame(iframe)
                ok = False
                try:
                    ok = d.execute_script(
                        "try{ var inp = document.querySelector(arguments[0]); if(!inp) return false; inp.focus && inp.focus(); inp.value = arguments[1]; inp.dispatchEvent(new Event('input',{bubbles:true})); return true; }catch(e){return false;}",
                        CVV_INPUT_SELECTOR, str(code)
                    )
                except Exception:
                    ok = False

                try:
                    d.switch_to.default_content()
                except Exception:
                    pass

                if ok:
                    try:
                        d.execute_script(f"try{{ window.localStorage.setItem('{CVV_CACHE_KEY}', arguments[0]); return true; }}catch(e){{return false;}}", str(idx))
                    except Exception:
                        pass
                    try:
                        d.execute_script(f"try{{ window.localStorage.setItem('{CVV_METHOD_MARKER_KEY}','js'); }}catch(e){{}}")
                    except Exception:
                        pass
                    print(f"[CVV] iframe index={idx} に JS で入力成功 -> キャッシュ保存", flush=True)
                    return True

                print(f"[CVV] iframe index={idx} JS書込不可（送信せず）→ 次の iframe を試行", flush=True)

            except Exception as e:
                try:
                    d.switch_to.default_content()
                except Exception:
                    pass
                print(f"[CVV] iframe index {idx} 試行で例外: {e}", flush=True)
                pass

            time.sleep(POLL_SHORT)

        time.sleep(POLL_SHORT)

    try:
        d.execute_script(f"try{{ window.localStorage.setItem('{CVV_METHOD_MARKER_KEY}','js_failed'); }}catch(e){{}}")
    except Exception:
        pass

    print("[CVV] iframe / 入力欄が出現せずまたは JS 書込失敗で timeout (JS-only)", flush=True)
    try:
        d.switch_to.default_content()
    except Exception:
        pass
    return False


# ====== postMessage リスナ注入（親ページで受け取る） ======
def install_cvv_postmessage_listener(d):
    script = r"""
    try{
      if(window._tdCvvListenerInstalled) { /* already */ }
      else {
        window._tdCvvListenerInstalled = true;
        window.addEventListener('message', function(ev){
          try{
            var data = ev.data;
            if(!data) return;
            var s = JSON.stringify(data);
            if(s.indexOf('cvv') !== -1 || s.indexOf('maskedCardDetails') !== -1 || s.indexOf('token') !== -1 || s.indexOf('cardinal') !== -1){
              try{ window.localStorage.setItem('td_cvv_token_received', s); }catch(e){}
              window._tdReceivedCvvToken = data;
            }
          }catch(e){}
        }, false);
      }
    }catch(e){}
    """
    try:
        d.execute_script(script)
    except Exception:
        pass


# ====== performance logs から token/支払い系URLを検出 ======
def drain_performance_logs_for_token(driver, timeout=1.0):
    start = time.perf_counter()
    seen_urls = set()
    while time.perf_counter() - start < timeout:
        try:
            logs = []
            try:
                logs = driver.get_log('performance')
            except Exception:
                logs = []
        except Exception:
            logs = []
        for entry in logs:
            try:
                msg = json.loads(entry['message'])['message']
                method = msg.get('method','')
                params = msg.get('params',{})
            except Exception:
                continue
            url = None
            if method.endswith('Network.requestWillBeSent'):
                req = params.get('request') or {}
                url = req.get('url')
            elif method.endswith('Network.responseReceived'):
                response = params.get('response') or {}
                url = response.get('url')
            else:
                url = (params.get('documentURL') or params.get('url'))
            if not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if _payment_re.search(url) or _payment_re.search(json.dumps(params)):
                print(f"[NETWORK DETECT] matched payment url: {url}", flush=True)
                return True
        time.sleep(0.01)
    return False


# ====== CVV確認ロジック（重要） ======
def _is_three_digit_str(s):
    if not s:
        return False
    s2 = str(s).strip()
    return len(s2) >= 3 and s2[-3:].isdigit()

def wait_for_cvv_confirmation(d, code=CVV_CODE, timeout=1.0):
    """
    優先: iframe内 input.value を直接読む（3桁確認、可能なら code の末尾3桁と一致確認）。
    フォールバック: localStorage/postMessage -> hidden input -> performance logs.
    戻り値: True=確認成功, False=タイムアウトで未確認
    """
    start = time.perf_counter()
    poll = FAST_POLL
    while time.perf_counter() - start < timeout:
        try:
            # 1) localStorage に postMessage token が残っているか
            try:
                token = d.execute_script("try{return window.localStorage.getItem('td_cvv_token_received');}catch(e){return null;}")
            except Exception:
                token = None
            if token:
                print("[CVV DETECT] postMessage token/localStorage detected", flush=True)
                return True

            # 2) iframe 内の input.value を直接読む（cache で js 書込みが行われている場合）
            try:
                method = d.execute_script(f"try{{return window.localStorage.getItem('{CVV_METHOD_MARKER_KEY}');}}catch(e){{return null;}}")
            except Exception:
                method = None

            try:
                iframes = d.find_elements(By.CSS_SELECTOR, CVV_IFRAME_SELECTOR)
            except Exception:
                iframes = []

            for idx, iframe in enumerate(iframes):
                try:
                    if not iframe.is_displayed():
                        continue
                    d.switch_to.frame(iframe)
                    val = d.execute_script(
                        "try{ var i = document.querySelector(arguments[0]); if(!i) return null; return (i.value||i.getAttribute('value')||null); }catch(e){return null;}",
                        CVV_INPUT_SELECTOR
                    )
                    try:
                        d.switch_to.default_content()
                    except Exception:
                        pass
                    if val:
                        vstr = str(val).strip()
                        if _is_three_digit_str(vstr):
                            # If CVV_CODE given, optionally verify endswith
                            if code:
                                if vstr.endswith(str(code)[-3:]):
                                    print(f"[CVV DETECT] iframe[{idx}] input.value read -> '{vstr}' (matches code end)", flush=True)
                                    return True
                                else:
                                    # 3 digits but doesn't match expected; still accept? Log and accept 3-digit
                                    if len(vstr) == 3 and vstr.isdigit():
                                        print(f"[CVV DETECT] iframe[{idx}] input.value read -> '{vstr}' (3 digits but not match expected; accepting 3-digit)", flush=True)
                                        return True
                            else:
                                print(f"[CVV DETECT] iframe[{idx}] input.value read -> '{vstr}' (3 digits)", flush=True)
                                return True
                except Exception:
                    try:
                        d.switch_to.default_content()
                    except Exception:
                        pass
                    pass
        except Exception:
            pass
        time.sleep(poll)
    print("[CVV DETECT] quick local confirm timeout", flush=True)
    return False


# ====== SUBMIT 多段リトライ JS ======
SUBMIT_JS = r"""
(function(){
 try{
  window.__TD_STOP=true;
  if(window.__TD_OB)window.__TD_OB.disconnect();
  if(window.__TD_TICK_TIMER){
    clearInterval(window.__TD_TICK_TIMER);
    window.__TD_TICK_TIMER=null;
  }
 }catch(e){}
 const b=document.evaluate(
  "//button[.//span[normalize-space(.)='申し込みを完了する']]",
  document,null,9,null
 ).singleNodeValue;
 if(b){
   try{ requestAnimationFrame(()=>b.click()); }catch(e){}
   try{ setTimeout(function(){ try{ b.click(); }catch(e){} }, 30); }catch(e){}
   try{ setTimeout(function(){ try{ b.click(); }catch(e){} }, 80); }catch(e){}
 }
})();
"""


# ====== CDPクリックユーティリティ ======
def cdp_click_element_if_possible(d, xpath):
    try:
        pos = d.execute_script(
            r"""
            try{
              var el = document.evaluate(arguments[0], document, null, 9, null).singleNodeValue;
              if(!el) return null;
              var r = el.getBoundingClientRect();
              return { x: (r.left + r.width/2)|0, y: (r.top + r.height/2)|0 };
            }catch(e){ return null; }
            """,
            xpath
        )
        if not pos:
            return False
        x = int(pos.get("x", 0))
        y = int(pos.get("y", 0))
        try:
            d.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            d.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        except Exception:
            return False
    except Exception:
        return False


# ====== JSON から発火時刻算出関数（compute_event_fire_local_dt） と run_at_local_datetime ======
def _parse_iso_utc(s):
    if not isinstance(s, str):
        raise ValueError("startApply is not string")
    if s.endswith("Z"):
        s2 = s[:-1] + "+00:00"
    else:
        s2 = s
    return datetime.datetime.fromisoformat(s2).astimezone(datetime.timezone.utc)


def _collect_start_apply_candidates(obj, out=None, path="root"):
    """
    eventDetail.ticketInfoList 固定ではなく、JSON全体から startApply を探す。
    イベントによって ticketInfoList の位置や構造が違う場合の対策。
    """
    if out is None:
        out = []

    if isinstance(obj, dict):
        if obj.get("startApply"):
            start_s = obj.get("startApply")
            try:
                dt_utc = _parse_iso_utc(start_s)
                out.append({
                    "dt_utc": dt_utc,
                    "receptionType": obj.get("receptionType", ""),
                    "status": obj.get("status", ""),
                    "raw": obj,
                    "path": path
                })
            except Exception:
                pass

        for k, v in obj.items():
            _collect_start_apply_candidates(v, out, f"{path}.{k}")

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect_start_apply_candidates(v, out, f"{path}[{i}]")

    return out


def _select_best_start_apply_candidate(candidates):
    if not candidates:
        raise RuntimeError("JSON内に startApply がありません")

    best = None

    # 最優先: receptionType=first かつ ended以外
    for c in candidates:
        if c.get("receptionType") != "first":
            continue
        if c.get("status") == "ended":
            continue
        if best is None or c["dt_utc"] < best["dt_utc"]:
            best = c

    # 次点: ended以外の最も早い startApply
    if best is None:
        for c in candidates:
            if c.get("status") == "ended":
                continue
            if best is None or c["dt_utc"] < best["dt_utc"]:
                best = c

    # 最後: 全候補から最も早い startApply
    if best is None:
        best = min(candidates, key=lambda x: x["dt_utc"])

    return best


def _compute_event_fire_from_json_obj(obj):
    candidates = _collect_start_apply_candidates(obj)
    best = _select_best_start_apply_candidate(candidates)

    dt_utc = best["dt_utc"]
    try:
        tgt_td_utc = dt_utc + datetime.timedelta(milliseconds=EVENT_SHIFT_REAL_MS)
    except Exception:
        tgt_td_utc = dt_utc

    global TD_OFFSET_SEC
    if TD_OFFSET_SEC is None:
        raise RuntimeError("TD_OFFSET_SEC が取得できず JSON 時刻は使用不可")

    local_fire_ts = tgt_td_utc.timestamp() - TD_OFFSET_SEC
    local_fire_dt = datetime.datetime.fromtimestamp(local_fire_ts)

    return local_fire_dt, best


def _get_page_props_from_next_data(d):
    return d.execute_script("""
        try {
            const raw = window.__NEXT_DATA__;
            if (!raw) return null;
            if (raw.props && raw.props.pageProps) return raw.props.pageProps;
            if (raw.pageProps) return raw.pageProps;
            return raw;
        } catch(e) {
            return null;
        }
    """)


def compute_event_fire_local_dt(d):
    """
    既存互換用。
    __NEXT_DATA__ 内を再帰探索して startApply を取得する。
    """
    page_props = _get_page_props_from_next_data(d)
    if not page_props:
        raise RuntimeError("__NEXT_DATA__ pageProps が取得できません")
    return _compute_event_fire_from_json_obj(page_props)


def fetch_next_event_json_obj(d):
    """
    現在URL / EVENT_URL の event_id と __NEXT_DATA__.buildId から
    /_next/data/<build>/event/<event_id>.json?id=<event_id>
    を直接fetchして startApply を拾う。
    """
    result = d.execute_async_script("""
        const done = arguments[0];

        (async () => {
            try {
                const href = location.href || arguments[0];
                const target = new URL(href);
                const m = target.pathname.match(/\\/event\\/([^/?#]+)/);
                if (!m) {
                    done({ok:false, reason:"url is not /event/<id>", href: target.href});
                    return;
                }

                const eventId = decodeURIComponent(m[1]);
                const buildId = window.__NEXT_DATA__ && window.__NEXT_DATA__.buildId;
                if (!buildId) {
                    done({ok:false, reason:"no buildId", href: target.href});
                    return;
                }

                const path =
                    "/_next/data/" + buildId +
                    "/event/" + encodeURIComponent(eventId) +
                    ".json?id=" + encodeURIComponent(eventId);

                const res = await fetch(path, {
                    credentials: "include",
                    cache: "no-store",
                    headers: {"accept": "application/json,text/plain,*/*"}
                });

                const txt = await res.text();
                done({
                    ok: res.ok,
                    status: res.status,
                    url: path,
                    text: txt.slice(0, 300000)
                });
            } catch(e) {
                done({ok:false, reason:String(e), href: location.href});
            }
        })();
    """, EVENT_URL)

    if not result or not result.get("ok"):
        raise RuntimeError(f"next data fetch failed: {result}")

    try:
        data = json.loads(result.get("text") or "{}")
    except Exception as e:
        raise RuntimeError(f"next data json parse failed: {e}")

    if isinstance(data, dict):
        if data.get("pageProps"):
            return data.get("pageProps")
        if data.get("props") and data["props"].get("pageProps"):
            return data["props"]["pageProps"]

    return data


def compute_event_fire_local_dt_best_effort(d, timeout=8.0):
    """
    1) __NEXT_DATA__ を再帰探索
    2) /_next/data/.../event/<id>.json を直接fetchして再帰探索
    の順で startApply を取る。
    """
    end = time.perf_counter() + timeout
    last_err = None

    while time.perf_counter() < end:
        try:
            local_dt, info = compute_event_fire_local_dt(d)
            print("[EVENT_TIME] source = __NEXT_DATA__", flush=True)
            return local_dt, info
        except Exception as e:
            last_err = e

        try:
            obj = fetch_next_event_json_obj(d)
            local_dt, info = _compute_event_fire_from_json_obj(obj)
            print("[EVENT_TIME] source = _next/data event json", flush=True)
            return local_dt, info
        except Exception as e:
            last_err = e

        time.sleep(POLL_SHORT)

    raise RuntimeError(f"startApply best-effort failed: {last_err}")

def run_at_local_datetime(target_dt, fn):
    if not isinstance(target_dt, datetime.datetime):
        raise ValueError("target_dt must be datetime")
    while datetime.datetime.now() < target_dt:
        time.sleep(0.0005)
    fn()



# ====== 最速リロード ======
def fast_reload_event_page(d):
    """
    発火時にイベントカードクリックは使わず、現在の EVENT_URL を最速再取得する。
    cache buster を付けて React/Next の状態再構築を狙う。
    """
    try:
        sep = "&" if "?" in EVENT_URL else "?"
        reload_url = f"{EVENT_URL}{sep}r={int(time.time() * 1000)}"
        d.execute_cdp_cmd("Page.navigate", {"url": reload_url})
        return True
    except Exception:
        try:
            sep = "&" if "?" in EVENT_URL else "?"
            reload_url = f"{EVENT_URL}{sep}r={int(time.time() * 1000)}"
            d.execute_script("location.replace(arguments[0]);", reload_url)
            return True
        except Exception:
            try:
                d.get(EVENT_URL)
                return True
            except Exception:
                return False


def wait_event_page_ready_after_reload(d, timeout=8.0):
    """
    リロード後、枚数セレクトまたは選択するボタンが見えるまで最短待機。
    以降の枚数選択処理は EARLY_JS 側をそのまま使う。
    """
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            ready = d.execute_script("""
                try {
                    const q = document.evaluate(
                        "//select[contains(@class,'TicketTypeCard_numberSelector__')]",
                        document, null, 9, null
                    ).singleNodeValue;
                    if (q) return true;

                    const b = document.evaluate(
                        "//button[contains(@class,'StageListItem_active__') and .//span[normalize-space(text())='選択する']]",
                        document, null, 9, null
                    ).singleNodeValue;
                    if (b) return true;

                    return document.readyState === "interactive" || document.readyState === "complete";
                } catch(e) {
                    return false;
                }
            """)
            if ready:
                return True
        except Exception:
            pass
        time.sleep(FAST_POLL)
    return False



# ====== ナビゲーション: ハンバーガー → お気に入り ======
def go_favorite_via_nav(d, timeout=4.0):
    """
    startApply 取得後に、ヘッダーのハンバーガーメニューを開いて
    「お気に入り」をクリックし、お気に入りページへ戻す。
    """
    try:
        btn = None
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            try:
                btn = d.find_element(By.CSS_SELECTOR, "button.NavHeader_button__3DYQn")
                if btn and btn.is_displayed():
                    break
            except Exception:
                btn = None
            time.sleep(FAST_POLL)

        if not btn:
            print("[PRELOAD] nav hamburger not found", flush=True)
            return False

        try:
            d.execute_script("arguments[0].click();", btn)
        except Exception:
            try:
                btn.click()
            except Exception:
                print("[PRELOAD] nav hamburger click failed", flush=True)
                return False

        fav = None
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            try:
                fav = d.find_element(By.XPATH, "//span[normalize-space()='お気に入り']")
                if fav and fav.is_displayed():
                    break
            except Exception:
                fav = None
            time.sleep(FAST_POLL)

        if not fav:
            print("[PRELOAD] favorite menu item not found", flush=True)
            return False

        try:
            d.execute_script("arguments[0].click();", fav)
        except Exception:
            try:
                fav.click()
            except Exception:
                print("[PRELOAD] favorite menu item click failed", flush=True)
                return False

        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            try:
                if "/favorite" in d.current_url:
                    print("[PRELOAD] favorite page opened", flush=True)
                    return True
            except Exception:
                pass
            time.sleep(FAST_POLL)

        print("[PRELOAD] favorite click done", flush=True)
        return True

    except Exception as e:
        print(f"[PRELOAD] favorite navigation failed: {e}", flush=True)
        return False




# ====== プリロード後: ハート → favorite直移動 → カードクリック → 戻る ======
def click_event_heart(d, timeout=4.0):
    """
    イベントページ上のハートボタンをクリックしてお気に入り登録する。
    指定クラス: div.EventInfoUnit_heart__MxRNG
    """
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            heart = d.find_element(By.CSS_SELECTOR, "div.EventInfoUnit_heart__MxRNG")
            if heart and heart.is_displayed():
                try:
                    d.execute_script("arguments[0].click();", heart)
                except Exception:
                    heart.click()
                print("[PRELOAD] heart clicked", flush=True)
                return True
        except Exception:
            pass
        time.sleep(FAST_POLL)

    print("[PRELOAD] heart not found/click failed", flush=True)
    return False


def go_favorite_direct(d, timeout=6.0):
    """
    https://ticketdive.com/favorite に直接移動する。
    """
    if not safe_get("https://ticketdive.com/favorite", retries=2):
        print("[PRELOAD] favorite direct get failed", flush=True)
        return False

    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        try:
            if "/favorite" in d.current_url:
                print("[PRELOAD] favorite page opened", flush=True)
                return True
        except Exception:
            pass
        time.sleep(FAST_POLL)

    print("[PRELOAD] favorite direct opened maybe", flush=True)
    return True


def preload_favorite_card_warm_and_back(d):
    """
    startApply取得・発火時間セット後のプリロード追加処理:
    1) イベントページでハートクリック
    2) favorite直URLへ移動
    3) 一番上のイベントカードをクリック
    4) favoriteへ戻る
    5) 発火時刻まで待機
    """
    try:
        click_event_heart(d, timeout=4.0)

        if not go_favorite_direct(d, timeout=6.0):
            return False

        # favorite上の一番上カードをJS側にキャッシュしてから、プリロード用にJSクリック
        cache_favorite_top_event_card_js(d, timeout=4.0)
        clicked = click_cached_favorite_card_js(d, timeout=1.0)
        if not clicked:
            # 万一JSキャッシュクリックが失敗した場合だけ、従来のSeleniumクリックへフォールバック
            print("[PRELOAD] favorite cached card JS click failed → fallback Selenium click", flush=True)
            clicked = click_top_event_card(d, timeout=4.0)
            if clicked:
                print("[PRELOAD] fallback Selenium click used", flush=True)
        if not clicked:
            print("[PRELOAD] fallback Selenium click also failed", flush=True)
            print("[PRELOAD] favorite top event card click failed", flush=True)
            return False

        print("[PRELOAD] favorite top event card JS clicked → back", flush=True)

        start = time.perf_counter()
        while time.perf_counter() - start < 4.0:
            try:
                if "/event/" in d.current_url:
                    break
            except Exception:
                pass
            time.sleep(FAST_POLL)

        try:
            d.back()
        except Exception:
            safe_get("https://ticketdive.com/favorite", retries=2)

        start = time.perf_counter()
        while time.perf_counter() - start < 5.0:
            try:
                if "/favorite" in d.current_url:
                    # 発火時にSelenium探索を使わず即クリックできるよう、戻った後に再キャッシュ
                    cache_favorite_top_event_card_js(d, timeout=2.0)
                    print("[PRELOAD] returned to favorite page (preload done)", flush=True)
                    return True
            except Exception:
                pass
            time.sleep(FAST_POLL)

        print("[PRELOAD] back to favorite timeout/maybe done", flush=True)
        return True

    except Exception as e:
        print(f"[PRELOAD] favorite warm/back failed: {e}", flush=True)
        return False




# ====== Next.js data JSON 事前fetch ======
def warm_next_event_jsons(d, timeout=5.0):
    """
    1ページ目 → 2ページ目遷移を軽くするため、
    startApply取得後に event.json / apply.json を事前fetchする。
    ※申込実行ではなく、Next.js data / ServiceWorker / 通信経路のウォーム目的。
    """
    try:
        result = d.execute_async_script("""
            const done = arguments[0];

            (async () => {
                const out = [];
                try {
                    const cur = new URL(location.href);
                    const m = cur.pathname.match(/\\/event\\/([^/?#]+)/);
                    const eventId = m ? decodeURIComponent(m[1]) : null;
                    const buildId = window.__NEXT_DATA__ && window.__NEXT_DATA__.buildId;

                    if (!eventId || !buildId) {
                        done({
                            ok: false,
                            reason: "missing eventId/buildId",
                            href: location.href,
                            eventId,
                            buildId
                        });
                        return;
                    }

                    const urls = [
                        `/_next/data/${buildId}/event/${encodeURIComponent(eventId)}.json?id=${encodeURIComponent(eventId)}`,
                        `/_next/data/${buildId}/event/${encodeURIComponent(eventId)}/apply.json?id=${encodeURIComponent(eventId)}`
                    ];

                    for (const url of urls) {
                        const t0 = performance.now();
                        try {
                            const res = await fetch(url, {
                                credentials: "include",
                                cache: "no-store",
                                headers: {
                                    "accept": "application/json,text/plain,*/*"
                                }
                            });

                            // bodyを読むことでServiceWorker/ブラウザ側に乗りやすくする
                            const text = await res.text();
                            const t1 = performance.now();

                            out.push({
                                url,
                                status: res.status,
                                ok: res.ok,
                                ms: Math.round(t1 - t0),
                                size: text.length
                            });
                        } catch(e) {
                            const t1 = performance.now();
                            out.push({
                                url,
                                status: 0,
                                ok: false,
                                ms: Math.round(t1 - t0),
                                error: String(e)
                            });
                        }
                    }

                    done({ok: true, results: out});
                } catch(e) {
                    done({ok: false, reason: String(e), results: out});
                }
            })();
        """)

        if not result or not result.get("ok"):
            print(f"[WARM] next data fetch failed: {result}", flush=True)
            return False

        ok_any = False
        for r in result.get("results", []):
            print(
                f"[WARM] {r.get('status')} {r.get('ms')}ms size={r.get('size', 0)} {r.get('url')}",
                flush=True
            )
            if r.get("ok"):
                ok_any = True

        return ok_any

    except Exception as e:
        print(f"[WARM] next data fetch exception: {e}", flush=True)
        return False



# ====== run_flow: CVVが3桁確認されるまで待ってからクリック ======
def run_flow(d):
    global fire_time, reload_start_dt
    win_label = "[WINDOW2]"
    reload_start_dt = datetime.datetime.now()
    fire_time = reload_start_dt
    log(f"{win_label} FIRE start {ts_human()}")

    # 1) 発火時はリロードせず、favoriteでJSキャッシュ済みの一番上イベントカードを即クリック
    try:
        clicked = click_cached_favorite_card_js(d, timeout=0.35)
        if clicked:
            log(f"{win_label} favorite cached card JS clicked")
        else:
            # キャッシュが切れていた場合のみ従来クリックへフォールバック
            log(f"{win_label} favorite cached card JS click failed → fallback Selenium click")
            if not click_top_event_card(d, timeout=1.0):
                log(f"{win_label} fallback Selenium click also failed")
                log(f"{win_label} イベントカードが見つからずクリック不可")
                return
            log(f"{win_label} fallback Selenium click used")
    except Exception as e:
        log(f"{win_label} イベントカードクリックエラー: {e}")
        return

    # EARLY_JS trigger
    # 事前fetch / SPA遷移後に監視状態がズレる対策として、
    # 発火時のイベントカードクリック後、枚数selectが出てから EARLY_JS を現在ページ上で完全リセットして再起動する。
    wait_for_quantity_select_after_card_click(d, timeout=1.5)
    try:
        d.execute_script("""
            try {
                window.__TD_STOP = true;
                if (window.__TD_OB) {
                    try { window.__TD_OB.disconnect(); } catch(e) {}
                    window.__TD_OB = null;
                }
                if (window.__TD_TICK_TIMER) {
                    try { clearInterval(window.__TD_TICK_TIMER); } catch(e) {}
                    window.__TD_TICK_TIMER = null;
                }
                window._tdInstalled = false;
                window.__TD_FIRE = false;
                window.__TD_QUANTITY_DONE = false;
                window.__TD_APPLY_DONE = false;
            } catch(e) {}
        """)
        d.execute_script(EARLY_JS)
        d.execute_script("""
            try {
                window.__TD_STOP = false;
                window.__TD_FIRE = true;
                window.__TD_QUANTITY_DONE = false;
                window.__TD_APPLY_DONE = false;
                if (window.__TD_FORCE_TICK) { window.__TD_FORCE_TICK(); }
            } catch(e) {}
        """)
        log(f"{win_label} EARLY_JS reset/fire start")
    except Exception as e:
        log(f"{win_label} FIRE失敗: {e}")
        return

    # 2) fill CVV (attempt JS-write)
    t_cvv_written = None
    try:
        t0 = now_ms()
        ok = fill_cvv_iframe(d)
        t1 = now_ms()
        if ok:
            t_cvv_written = t1
            log(f"[TIMING] cvv_written at {t_cvv_written} ({(t1-t0)*1000:.1f}ms after fill)")
        else:
            log(f"{win_label} CVV JS fill not completed")
    except Exception as e:
        log(f"{win_label} fill_cvv exception: {e}")
        return

    # 3) CVV確認フェーズ（3桁確認を優先）
    confirmed = False
    detect_start = now_ms()
    try:
        # Try quick local confirmation (read iframe input)
        if wait_for_cvv_confirmation(d, code=CVV_CODE, timeout=QUICK_DETECT_TIMEOUT):
            confirmed = True
            log(f"{win_label} CVV confirmation: local quick-detect ({(now_ms()-detect_start)*1000:.1f}ms)")
        else:
            # Try perf logs (network) short
            if drain_performance_logs_for_token(d, timeout=NETWORK_DETECT_TIMEOUT):
                confirmed = True
                log(f"{win_label} CVV confirmation: network detect ({(now_ms()-detect_start)*1000:.1f}ms)")
            else:
                # As a stronger attempt, do a longer local read up to TOTAL_DETECTION_TIMEOUT
                remaining = max(0.0, TOTAL_DETECTION_TIMEOUT - (now_ms() - detect_start))
                if remaining > 0:
                    if wait_for_cvv_confirmation(d, code=CVV_CODE, timeout=remaining):
                        confirmed = True
                        log(f"{win_label} CVV confirmation: local extended-detect ({(now_ms()-detect_start)*1000:.1f}ms)")
    except Exception as e:
        log(f"{win_label} cvv detection exception: {e}")

    # 4) Submit button enable-watch
    submit_xpath = "//button[.//span[normalize-space(.)='申し込みを完了する']]"
    def is_submit_enabled_local(dr):
        try:
            return dr.execute_script(
                "try{ var b=document.evaluate(arguments[0], document, null, 9, null).singleNodeValue; if(!b) return false; if(b.disabled) return false; if(b.getAttribute && b.getAttribute('aria-disabled')==='true') return false; return true; }catch(e){return false;}",
                submit_xpath
            )
        except Exception:
            return False

    clicked_by_enabled_watch = False
    try:
        st = time.perf_counter()
        enabled = False
        while time.perf_counter() - st < SUBMIT_ENABLED_TIMEOUT:
            if is_submit_enabled_local(d):
                enabled = True
                break
            time.sleep(0.005)
        if enabled:
            # If confirmed -> click fast; if not confirmed yet -> wait a tiny bit longer if detection still running
            if confirmed or True:
                # prefer CDP click if available
                if cdp_click_element_if_possible(d, submit_xpath):
                    clicked_by_enabled_watch = True
                    log(f"{win_label} SUBMIT clicked (by enabled-watch CDP)")
                else:
                    try:
                        d.execute_script("try{ var b=document.evaluate(arguments[0], document, null, 9, null).singleNodeValue; if(b){ requestAnimationFrame(()=>b.click()); } }catch(e){}", submit_xpath)
                        clicked_by_enabled_watch = True
                        log(f"{win_label} SUBMIT clicked (by enabled-watch JS)")
                    except Exception as e:
                        log(f"{win_label} enabled-watch click error: {e}")
                        clicked_by_enabled_watch = False
    except Exception as e:
        log(f"{win_label} enabled-watch exception: {e}")
        clicked_by_enabled_watch = False

    # 5) If not clicked by enabled-watch, decide pressing strategy based on confirmed flag
    if not clicked_by_enabled_watch:
        if confirmed:
            try:
                # confirmed -> click (fast)
                if cdp_click_element_if_possible(d, submit_xpath):
                    log(f"{win_label} SUBMIT clicked (confirmed path, CDP)")
                else:
                    d.execute_script(SUBMIT_JS)
                    log(f"{win_label} SUBMIT clicked (confirmed path, JS)")
            except Exception as e:
                log(f"{win_label} SUBMIT click error (confirmed path): {e}")
        else:
            # Not confirmed: prefer to wait a small additional window up to TOTAL_DETECTION_TIMEOUT, else fallback to best-effort click but log warning
            t_wait_start = time.perf_counter()
            waited = 0.0
            extra_wait_limit = 0.5  # 追加で待つ最大時間（必要なら増やす）
            got_confirm = False
            while time.perf_counter() - t_wait_start < extra_wait_limit:
                if wait_for_cvv_confirmation(d, code=CVV_CODE, timeout=0.08):
                    got_confirm = True
                    break
                if drain_performance_logs_for_token(d, timeout=0.08):
                    got_confirm = True
                    break
            if got_confirm:
                try:
                    d.execute_script(SUBMIT_JS)
                    log(f"{win_label} SUBMIT clicked (late-confirm path)", flush=True)
                except Exception as e:
                    log(f"{win_label} SUBMIT click error (late-confirm): {e}")
            else:
                # 最終フォールバック：警告ログを出してから強制クリック（運用方針に応じてここを禁止可）
                log(f"{win_label} WARNING: CVV not confirmed after wait → performing fallback click (may be premature)", flush=True)
                try:
                    d.execute_script(SUBMIT_JS)
                    log(f"{win_label} SUBMIT clicked (fallback no-confirm)", flush=True)
                except Exception as e:
                    log(f"{win_label} SUBMIT click error (fallback): {e}", flush=True)

    # 6) wait for URL change / result
    try:
        base_url = d.current_url
    except Exception:
        base_url = None

    start_wait = time.perf_counter()
    while time.perf_counter() - start_wait < URL_CHANGE_TIMEOUT:
        try:
            cur = d.current_url
        except Exception:
            log(f"{win_label} FIRE→EXCEPTION while waiting URL change", flush=True)
            return
        if base_url is None:
            base_url = cur
        elif cur != base_url:
            end_dt = datetime.datetime.now()
            diff = (end_dt - reload_start_dt).total_seconds()
            log(f"{win_label} FIRE→URL_CHANGE {diff:.3f}s (ts={ts_human()})")
            return
        time.sleep(0.01)

    end_dt = datetime.datetime.now()
    diff = (end_dt - reload_start_dt).total_seconds()
    log(f"{win_label} FIRE→TIMEOUT {diff:.3f}s (URL変化検知せず)")


# ====== Preload / run_event / main (Edge版同様) ======
def run_at(t, fn):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    tgt = datetime.datetime.strptime(today + " " + t, "%Y-%m-%d %H:%M:%S.%f")
    while datetime.datetime.now() < tgt:
        time.sleep(0.0005)
    fn()

def run_at_td(t, fn):
    global TD_OFFSET_SEC
    if TD_OFFSET_SEC is None:
        print("[WARN] TD_OFFSET_SEC=None → run_at_td はローカル時刻で動かす", flush=True)
        run_at(t, fn)
        return
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    tgt_td = datetime.datetime.strptime(today + " " + t, "%Y-%m-%d %H:%M:%S.%f")
    tgt_local = tgt_td - datetime.timedelta(seconds=TD_OFFSET_SEC)
    while datetime.datetime.now() < tgt_local:
        time.sleep(0.0005)
    fn()

def run_preload():
    global TD_OFFSET_SEC, EVENT_FIRE_LOCAL_DT, driver
    print("[PRELOAD] window preload", flush=True)
    try:
        sync_os_time_now()
    except Exception as e:
        print(f"[NTP] failed {e}", flush=True)
    try:
        TD_OFFSET_SEC = measure_ticketdive_offset()
        print(f"[TD_OFFSET] = {TD_OFFSET_SEC:+.3f}s", flush=True)
    except Exception as e:
        print(f"[TD_OFFSET] failed ({e}) → EVENT_AT フォールバック", flush=True)
        TD_OFFSET_SEC = None
    print(f"[PRELOAD] driver.get({EVENT_URL})", flush=True)
    if not safe_get(EVENT_URL, retries=3):
        print("[PRELOAD] driver.get FAILED", flush=True)
        return
    print("[PRELOAD] driver.get DONE", flush=True)
    # プリロードAT後はEVENT_URLでstartApplyを取得。
    # 発火時間セット後の favorite 移動・カードウォーム・戻る処理は main() 側で実行する。
    print("[PRELOAD] event page loaded for startApply", flush=True)
    if not PRACTICE_MODE and TD_OFFSET_SEC is not None:
        try:
            EVENT_FIRE_LOCAL_DT, info = compute_event_fire_local_dt_best_effort(driver, timeout=8.0)
            print("[EVENT_TIME] startApply(UTC) =", info["dt_utc"], flush=True)
            print("[EVENT_TIME] startApply path =", info.get("path", ""), flush=True)
            print("[EVENT_TIME] local_fire =", EVENT_FIRE_LOCAL_DT, flush=True)
        except Exception as e:
            print(f"[EVENT_TIME] JSON failed {e}", flush=True)
            EVENT_FIRE_LOCAL_DT = None

    # 練習・本番どちらでも同じ条件にするため、event.json / apply.json を事前fetch
    # ※PRACTICE_MODE=True でも [WARM] ログが出れば、本番と同じfetch状態で検証できる。
    try:
        warm_next_event_jsons(driver, timeout=5.0)
    except Exception as e:
        print(f"[WARM] skipped/failed: {e}", flush=True)

    # お気に入り移動は、発火時刻セット完了後に main() 側で実行する。

def run_event():
    print("[EVENT] window fire", flush=True)
    run_flow(driver)

def main():
    global driver, EVENT_FIRE_LOCAL_DT, TD_OFFSET_SEC
    relaunch_as_admin()
    boost_timer_resolution_1ms()
    boost_process_priority_high()
    print("[LAUNCH] window2 (Chrome Selenium)", flush=True)
    driver = init_driver(PROFILE, PORT)
    boost_browser_priority_high(driver)
    install_early_js(driver)
    install_cvv_postmessage_listener(driver)
    if PRELOAD_EVENT_ENABLED:
        run_at(PRELOAD_EVENT_AT, run_preload)
    if PRACTICE_MODE:
        print(f"[MODE] PRACTICE=True EVENT_AT={EVENT_AT}", flush=True)
        # PRACTICE_MODE=True の場合は EVENT_AT が発火時刻。
        # 発火時刻セット完了後、待機に入る前に
        # ハート → favorite直移動 → 一番上カードクリック → 戻る を実行する。
        preload_favorite_card_warm_and_back(driver)
        run_at_td(EVENT_AT, run_event)
        return
    print("[MODE] PRACTICE=False (本番)", flush=True)
    if TD_OFFSET_SEC is None:
        print("[FALLBACK] TD_OFFSET_SEC 無し → EVENT_AT を TD 時刻として run_at_td", flush=True)
        preload_favorite_card_warm_and_back(driver)
        run_at_td(EVENT_AT, run_event)
        return
    if EVENT_FIRE_LOCAL_DT is None:
        try:
            EVENT_FIRE_LOCAL_DT, info = compute_event_fire_local_dt_best_effort(driver, timeout=8.0)
            print("[EVENT_TIME] (late) startApply(UTC) =", info["dt_utc"], flush=True)
            print("[EVENT_TIME] (late) startApply path =", info.get("path", ""), flush=True)
            print("[EVENT_TIME] (late) local_fire =", EVENT_FIRE_LOCAL_DT, flush=True)
        except Exception as e:
            print(f"[EVENT_TIME] late JSON failed {e}", flush=True)
            print("[FALLBACK] EVENT_AT へフォールバック", flush=True)
            # startApply取得失敗でEVENT_ATフォールバックの場合も、発火前にfavoriteへ戻す
            preload_favorite_card_warm_and_back(driver)
            run_at_td(EVENT_AT, run_event)
            return
    print(f"[EVENT] wait until {EVENT_FIRE_LOCAL_DT}", flush=True)
    # 本番時は startApply から発火時刻をセットした後、待機に入る前に
    # ハート → favorite直移動 → 一番上カードクリック → 戻る を実行する。
    preload_favorite_card_warm_and_back(driver)
    run_at_local_datetime(EVENT_FIRE_LOCAL_DT, run_event)

if __name__ == "__main__":
    main()