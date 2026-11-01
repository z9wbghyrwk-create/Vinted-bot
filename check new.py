"""Vinted phone watcher for GitHub Actions.

Runs once per invocation: reads searches.json, looks for NEW listings on Vinted,
sends them to you on Telegram, and remembers what it has seen in seen.json.
"""
import html
import json
import os
import random
import time

import httpx

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DOMAIN = os.getenv("VINTED_DOMAIN") or "be"

MAX_ALERTS_PER_SEARCH = 10
KEEP_IDS = 300
ERROR_NOTICE_EVERY = 6 * 3600  # seconds between "Vinted is blocking me" messages

# Vinted retired /api/v2/catalog/items around Sept 2026; the new one is tried first.
ENDPOINTS = ["/svc-catalogue/items", "/api/v2/catalog/items"]
LIST_KEYS = ("items", "catalog_items", "results", "products")

# Listings whose title contains any of these are skipped (accessories etc.)
DEFAULT_EXCLUDE = (
    "coque,case,hoesje,housse,étui,etui,cover,bumper,verre trempé,"
    "screen protector,protège,chargeur,charger,câble,cable,oplader,"
    "support,holder,stylet,pen,sticker,skin,lot de coques"
)
EXCLUDE = [w.strip().lower() for w in (os.getenv("EXCLUDE_WORDS") or DEFAULT_EXCLUDE).split(",") if w.strip()]

SEARCHES_FILE = "searches.json"
STATE_FILE = "seen.json"


# ----------------------------------------------------------------- Telegram
def tg(method: str, **data) -> bool:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=20)
    except httpx.HTTPError as e:
        print(f"Telegram {method} network error: {type(e).__name__}")
        return False
    if r.status_code != 200:
        print(f"Telegram {method} failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    return True


# ------------------------------------------------------------------- Vinted
def describe(obj) -> str:
    if isinstance(obj, dict):
        return "keys=" + ",".join(list(obj)[:8])
    return type(obj).__name__


def find_items(obj, depth=0):
    """Find the list of listings inside a JSON response, whatever its exact shape."""
    if depth > 4:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj
        return None
    if isinstance(obj, dict):
        for k in LIST_KEYS:
            if isinstance(obj.get(k), list):
                return obj[k]  # may legitimately be empty
        for v in obj.values():
            found = find_items(v, depth + 1)
            if found is not None:
                return found
    return None


def unwrap(x: dict) -> dict:
    if "id" not in x and isinstance(x.get("item"), dict):
        return x["item"]
    return x


class Vinted:
    def __init__(self, domain: str):
        self.base = f"https://www.vinted.{domain}"
        self.client = httpx.Client(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7",
            },
            follow_redirects=True,
            timeout=20,
        )
        self.ready = False
        self.refresh_status = None
        self.endpoint = None  # remembered once one of them works

    def refresh(self):
        """Vinted needs session cookies, which we get by loading the homepage."""
        self.client.cookies.clear()
        r = self.client.get(self.base + "/")
        self.refresh_status = r.status_code
        self.ready = r.status_code == 200
        print(f"Session refresh -> HTTP {r.status_code}")

    def _get(self, path: str, params: dict):
        r = self.client.get(self.base + path, params=params)
        if r.status_code in (401, 403):
            self.refresh()
            r = self.client.get(self.base + path, params=params)
        return r

    def search(self, query: str, max_price):
        if not self.ready:
            self.refresh()
        params = {"search_text": query, "order": "newest_first", "per_page": 30, "page": 1}
        if max_price:
            params["price_to"] = max_price
        paths = [self.endpoint] if self.endpoint else ENDPOINTS
        last = None
        for path in paths:
            r = self._get(path, params)
            if r.status_code == 404:
                print(f"{path} -> 404")
                last = r
                continue
            r.raise_for_status()
            data = r.json()
            items = find_items(data)
            if items is None:
                raise ValueError(f"unexpected response from {path}: {describe(data)}")
            self.endpoint = path
            return [unwrap(x) for x in items if isinstance(x, dict)]
        self.endpoint = None
        last.raise_for_status()


def parse_price(item):
    p = item.get("price")
    if isinstance(p, dict):
        return float(p.get("amount", 0) or 0), p.get("currency_code", "EUR")
    try:
        return float(p), item.get("currency", "EUR")
    except (TypeError, ValueError):
        pass
    try:
        return float(item.get("price_numeric")), item.get("currency", "EUR")
    except (TypeError, ValueError):
        return 0.0, "EUR"


def photo_url(item):
    p = item.get("photo")
    if isinstance(p, dict):
        return p.get("url") or p.get("full_size_url")
    photos = item.get("photos")
    if isinstance(photos, list) and photos and isinstance(photos[0], dict):
        return photos[0].get("url")
    return None


def is_excluded(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in EXCLUDE)


def send_alert(base_url: str, item: dict, query: str):
    price, cur = parse_price(item)
    title = html.escape(item.get("title", "?"))
    url = item.get("url") or f"{base_url}/items/{item['id']}"
    if url.startswith("/"):
        url = base_url + url
    extra = " · ".join(
        html.escape(str(x)) for x in (item.get("brand_title"), item.get("status"), item.get("size_title")) if x
    )
    caption = (
        f"📱 <b>{title}</b>\n"
        f"💶 <b>{price:.2f} {cur}</b>\n"
        + (f"🏷 {extra}\n" if extra else "")
        + f"🔎 {html.escape(query)}\n"
        f'<a href="{html.escape(url)}">Open on Vinted</a>'
    )
    photo = photo_url(item)
    if photo and tg("sendPhoto", chat_id=CHAT_ID, photo=photo, caption=caption[:1024], parse_mode="HTML"):
        return
    tg("sendMessage", chat_id=CHAT_ID, text=caption, parse_mode="HTML")


# --------------------------------------------------------------------- main
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main():
    searches = load_json(SEARCHES_FILE, [])
    old_state = load_json(STATE_FILE, {})
    meta = old_state.get("_meta", {})
    vinted = Vinted(DOMAIN)

    keys = [f"{s['query']}|{s.get('max_price')}" for s in searches]
    state = {k: old_state[k] for k in keys if k in old_state}  # drops removed searches
    failures = 0
    last_error = ""

    for s, key in zip(searches, keys):
        query, max_price = s["query"], s.get("max_price")
        try:
            items = vinted.search(query, max_price)
        except Exception as e:
            failures += 1
            if isinstance(e, httpx.HTTPStatusError):
                last_error = f"HTTP {e.response.status_code}"
            elif isinstance(e, ValueError):
                last_error = str(e)[:200]
            else:
                last_error = type(e).__name__
            last_error += f" (session HTTP {vinted.refresh_status})"
            print(f"Search '{query}' failed: {last_error}")
            continue

        items = [it for it in items if it.get("id") is not None]
        if max_price:  # in case Vinted ignores the price filter
            items = [it for it in items if parse_price(it)[0] <= float(max_price)]

        seen = state.get(key)  # None = first time we see this search
        known = set(seen or [])
        new = [it for it in items if it["id"] not in known]

        if seen is None:
            print(f"'{query}': first run, remembering {len(items)} existing listings")
        else:
            sent = 0
            for it in reversed(new):  # oldest first
                if is_excluded(it.get("title", "")):
                    continue
                if sent >= MAX_ALERTS_PER_SEARCH:
                    break
                send_alert(vinted.base, it, query)
                sent += 1
                time.sleep(0.5)
            print(f"'{query}': {len(new)} new, {sent} alerts sent")

        state[key] = ([it["id"] for it in new] + (seen or []))[:KEEP_IDS]
        time.sleep(random.uniform(2, 4))  # be gentle with Vinted

    if searches and failures == len(searches):
        now = time.time()
        if now - meta.get("err_notified_at", 0) > ERROR_NOTICE_EVERY:
            tg(
                "sendMessage",
                chat_id=CHAT_ID,
                text=f"⚠️ Vinted bot: every search failed: {last_error}. "
                "I'll keep trying quietly.",
            )
            meta["err_notified_at"] = now

    state["_meta"] = meta
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)


if __name__ == "__main__":
    main()
