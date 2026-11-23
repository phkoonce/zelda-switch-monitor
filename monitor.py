#!/usr/bin/env python3
"""Poll product pages; push an ntfy notification (with link) when one comes in stock.

Per-site optional keys: "in_stock_regex", "out_of_stock_regex" (case-insensitive).
Default detection: schema.org availability in JSON-LD/meta, then German/English keywords.
"""
import json, os, re, sys, time, urllib.request, urllib.error, http.cookiejar
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
if (HERE / "local.json").exists():                 # untracked local overrides (e.g. ntfy_topic)
    CFG.update(json.loads((HERE / "local.json").read_text()))
if os.environ.get("NTFY_TOPIC"):                   # GitHub Actions secret
    CFG["ntfy_topic"] = os.environ["NTFY_TOPIC"]
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
DRY = "--dry-run" in sys.argv
TEST = "--test-push" in sys.argv

IN_RE = re.compile(r'schema\.org/InStock|schema\.org/LimitedAvailability|"availability"\s*:\s*"?(?:https?://schema\.org/)?InStock', re.I)
OUT_RE = re.compile(r'schema\.org/(?:OutOfStock|SoldOut|Discontinued)|"availability"\s*:\s*"?(?:https?://schema\.org/)?(?:OutOfStock|SoldOut)|ausverkauft|nicht verfügbar|derzeit nicht lieferbar|out of stock|sold out|nicht lieferbar|vergriffen', re.I)
BUY_RE = re.compile(r'in den warenkorb|add to cart|jetzt kaufen|in den einkaufswagen|buy now', re.I)

def push(title, msg, url=None, priority="default"):
    print(f"[push:{priority}] {title} - {msg}")
    if DRY:
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "video_game"}
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, Open page, {url}"
    req = urllib.request.Request(f"https://ntfy.sh/{CFG['ntfy_topic']}", data=msg.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("ntfy error:", e)

def fetch(url):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={"User-Agent": CFG["user_agent"], "Accept-Language": "de-CH,de;q=0.9,en;q=0.8"})
    with op.open(req, timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")

_pw = {}
def fetch_browser(url):
    """Real headless Chromium: passes JS cookie checks and Queue-it waiting rooms that let browsers through."""
    from playwright.sync_api import sync_playwright
    if "b" not in _pw:
        _pw["p"] = sync_playwright().start()
        _pw["b"] = _pw["p"].chromium.launch()
    ctx = _pw["b"].new_context(user_agent=CFG["user_agent"], locale="de-CH")
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + 60          # give a soft queue up to 60s to pass us through
        while "queue" in page.url.split("/")[2] and time.time() < deadline:
            page.wait_for_timeout(2000)
        if "queue" not in page.url.split("/")[2]:
            try:
                page.wait_for_function("document.body && document.body.innerText.length > 800", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
        return page.url, page.content()
    finally:
        ctx.close()

def classify(site, final_url, html):
    if "queue-" in final_url and "queue" in final_url.split("/")[2] or "queue-it" in final_url:
        return "queue"
    if len(html) < 4000 and "document.cookie" in html:
        return "blocked"
    if site.get("in_if_out_absent") and site.get("out_of_stock_regex"):
        return "out" if re.search(site["out_of_stock_regex"], html, re.I) else "in"
    if site.get("in_stock_regex") or site.get("out_of_stock_regex"):
        if site.get("out_of_stock_regex") and re.search(site["out_of_stock_regex"], html, re.I):
            return "out"
        if site.get("in_stock_regex") and re.search(site["in_stock_regex"], html, re.I):
            return "in"
        return "unknown"
    # Prefer the structured offer whose url is this page (ignores "recommended products" offers).
    here = final_url.split("?")[0].rstrip("/")
    for avail, ourl in re.findall(r'"availability"\s*:\s*"(?:https?://schema\.org/)?(\w+)"[^{}]*?"url"\s*:\s*"([^"]+)"', html):
        if ourl.split("?")[0].rstrip("/") == here:
            if avail in ("InStock", "LimitedAvailability", "PreOrder", "BackOrder", "OnlineOnly"):
                return "in"
            if avail in ("OutOfStock", "SoldOut", "Discontinued"):
                return "out"
    if OUT_RE.search(html) and not IN_RE.search(html):
        return "out"
    return "unknown"   # never guess "in" from loose keywords: too many false alarms

def zurich_awake():
    """True during 07:00-21:59 Europe/Zurich (handles DST). Set ACTIVE_HOURS_ONLY=1 to enforce."""
    if os.environ.get("ACTIVE_HOURS_ONLY") != "1":
        return True
    from zoneinfo import ZoneInfo
    return 7 <= datetime.now(ZoneInfo("Europe/Zurich")).hour < 22

def main():
    awake = zurich_awake()
    if not TEST and not awake and not any(s.get("any_hour") for s in CFG["sites"]):
        print("outside 07-22 Zurich, skipping")
        return
    if TEST:
        push("Zelda monitor test", "Push notifications work.", "https://store.nintendo.com/de-ch/", "default")
        return
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for site in CFG["sites"]:
        name, url = site["name"], site["url"]
        if not awake and not site.get("any_hour"):   # Swiss shops: Zurich daytime only; US shops run 24h
            continue
        last = state.get("_t:" + name, 0)
        if not os.environ.get("MIN_GAP_OFF") and time.time() - last < site.get("min_interval_sec", 0) - 5:
            continue
        state["_t:" + name] = time.time()
        try:
            final_url, html = (fetch_browser if site.get("browser") else fetch)(url)
            status = classify(site, final_url, html)
        except Exception as e:
            status = "error"
            print(f"{name}: {e}")
        prev = state.get(name)
        print(f"{time.strftime('%H:%M:%S')} {name}: {prev} -> {status}")
        if status == "in" and prev != "in":
            push(f"IN STOCK: {name}", "Zelda Switch 2 may be available now. Tap to open.", url, "urgent")
        elif status in ("queue", "blocked", "error", "unknown") and prev != status and prev is not None:
            push(f"{name}: can't read page ({status})", "Check the page manually or fix the config.", url, "low")
        elif prev is None and status in ("blocked", "error", "unknown"):
            push(f"{name}: can't read page ({status})", "Check the config.", url, "low")
        state[name] = status
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if "b" in _pw:
        _pw["b"].close(); _pw["p"].stop()

if __name__ == "__main__":
    main()
