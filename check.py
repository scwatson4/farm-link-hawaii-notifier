#!/usr/bin/env python3
"""Farm Link Hawaii watchlist notifier.

Polls Shopify collection JSON endpoints for farmlinkhawaii.com, applies
watchlist rules from watchlist.yaml, and posts matches to a Discord webhook.
State is persisted in state/seen.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

# The `produce` and `drinks` smart collections on this Shopify store return
# empty via /collections/<handle>/products.json (see README). We fetch
# `all-products` as the canonical universe and the leaves below purely to label
# each product with logical `produce` / `drinks` membership.
CONFIG: dict[str, Any] = {
    "store_base": "https://farmlinkhawaii.com",
    "user_agent": (
        "farm-link-hawaii-notifier/1.0 "
        "(+https://github.com/scwatson4/farm-link-hawaii-notifier)"
    ),
    "universal_collection": "all-products",
    "collection_groups": {
        "produce": [
            # Fruit-centric leaves. Farm Link Hawaii's handles are quirky:
            # `tropical` is actually the Avocado collection, and `tropical-1`
            # is the real Tropical collection (where pineapples live).
            "mango", "papaya", "berries", "tropical", "tropical-1",
            "bananas-1", "citrus", "seasonal-picks",
            "frozen-fruit", "frozen-fruit-vegetables", "frozen-produce-vegetables",
            "organic-fruit",
            # Vegetable-centric leaves.
            "bulk-produce", "leafy-greens", "lettuce", "greens",
            "herbs-1", "organic-herbs",
            "cucumbers", "cucumbers-1", "squash", "cabbage-1",
            "carrots", "beets", "onions", "onions-root-vegetables",
            "peppers", "eggplant", "tomatoes", "peas-beans", "potatoes",
            "radishes", "mushrooms", "sprouts-microgreens", "other-roots",
            "salad-mixes", "specialty-veg",
            "organic-greens", "organic-vegetables",
            "canoe-crops", "limu-and-seaweed",
        ],
        "drinks": [
            "juice", "kombucha", "mixers", "ready-to-drink", "soda-mixers",
            "coffee", "coffee-teas", "coffee-beans",
            "teas", "loose-leaf-bagged-tea",
            "plant-based-milks", "milk", "water-seltzer",
            "hard-cider-tea-kombucha", "pre-mixed-cocktails", "ready-made-coffee",
        ],
    },
    "page_limit": 250,
    "max_pages": 50,
    "request_timeout": 30,
    "discord_max_embeds": 10,
    "discord_webhook_username": "Farm Link Hawaii Watch",
}

REPO_ROOT = Path(__file__).resolve().parent
WATCHLIST_PATH = REPO_ROOT / "watchlist.yaml"
STATE_PATH = REPO_ROOT / "state" / "seen.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": CONFIG["user_agent"], "Accept": "application/json"})
    return s


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=CONFIG["request_timeout"])
        except requests.RequestException as e:
            last_exc = e
        else:
            if r.status_code in (429, 502, 503, 504):
                last_exc = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            else:
                r.raise_for_status()
                return r.json()
        if attempt < 3:
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def fetch_collection_products(session: requests.Session, handle: str) -> list[dict[str, Any]]:
    """Paginate /collections/{handle}/products.json until empty or max_pages."""
    out: list[dict[str, Any]] = []
    for page in range(1, CONFIG["max_pages"] + 1):
        url = (
            f"{CONFIG['store_base']}/collections/{handle}/products.json"
            f"?limit={CONFIG['page_limit']}&page={page}"
        )
        data = get_json(session, url)
        batch = data.get("products", [])
        if not batch:
            break
        out.extend(batch)
        if len(batch) < CONFIG["page_limit"]:
            break
    return out


def get_collection_meta(session: requests.Session, handle: str) -> dict[str, Any]:
    url = f"{CONFIG['store_base']}/collections/{handle}.json"
    return get_json(session, url).get("collection", {}) or {}


def validate_collection_groups(session: requests.Session) -> None:
    """Loud failure if a logical group has zero reachable, populated leaves.
    Transient HTTP errors on individual leaves are tolerated as long as at
    least one leaf per group comes back populated."""
    bad: list[str] = []
    for group, leaves in CONFIG["collection_groups"].items():
        any_populated = False
        missing: list[str] = []
        errored: list[str] = []
        for leaf in leaves:
            try:
                meta = get_collection_meta(session, leaf)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    missing.append(leaf)
                else:
                    errored.append(leaf)
                continue
            except requests.RequestException:
                errored.append(leaf)
                continue
            if (meta.get("products_count") or 0) > 0:
                any_populated = True
        if not any_populated:
            bad.append(
                f"group '{group}' has no reachable populated leaves "
                f"(checked {len(leaves)}; missing={missing}; errored={errored})"
            )
    if bad:
        msg = (
            "collection_groups validation failed — check if Farm Link Hawaii "
            "has renamed collections:\n  - " + "\n  - ".join(bad)
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(2)


def product_available(product: dict[str, Any]) -> bool:
    return any(v.get("available") for v in product.get("variants", []))


# Farm Link Hawaii's /products.json returns stale `available: true` for
# products whose storefront button is actually disabled with "Sold out".
# Authoritative signal: the <button name="add" ...> tag's `disabled` attr.
_ADD_BUTTON_RE = re.compile(r'<button[^>]*\bname="add"[^>]*>', re.IGNORECASE)


def confirm_in_stock(session: requests.Session, handle: str) -> bool:
    """Fetch the product's storefront HTML and confirm the add-to-cart button
    is enabled. Fail closed on network errors (treat as not in stock) so a
    Shopify blip can't produce false-positive alerts."""
    url = f"{CONFIG['store_base']}/products/{handle}"
    text: str | None = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=CONFIG["request_timeout"])
        except requests.RequestException:
            pass
        else:
            if r.status_code == 200:
                text = r.text
                break
            if r.status_code not in (429, 502, 503, 504):
                return False
        if attempt < 3:
            time.sleep(2 ** attempt)
    if text is None:
        return False
    m = _ADD_BUTTON_RE.search(text)
    if not m:
        return False
    return "disabled" not in m.group(0).lower()


def product_price_range(product: dict[str, Any]) -> tuple[float, float] | None:
    prices: list[float] = []
    for v in product.get("variants", []):
        p = v.get("price")
        if p is None:
            continue
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None
    return (min(prices), max(prices))


def format_price(price_range: tuple[float, float] | None) -> str:
    if price_range is None:
        return "n/a"
    lo, hi = price_range
    if lo == hi:
        return f"${lo:,.2f}"
    return f"${lo:,.2f} – ${hi:,.2f}"


def build_current_products(session: requests.Session) -> dict[str, dict[str, Any]]:
    """Return {product_id_str: product_record}. Fetches all-products + leaves,
    and labels each product with logical group memberships."""
    universal = fetch_collection_products(session, CONFIG["universal_collection"])
    print(f"fetched {len(universal)} products from '{CONFIG['universal_collection']}'")

    memberships: dict[str, set[str]] = {}
    extras: dict[str, dict[str, Any]] = {}  # products only found in leaves
    for group, leaves in CONFIG["collection_groups"].items():
        group_total = 0
        for leaf in leaves:
            try:
                leaf_products = fetch_collection_products(session, leaf)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                print(f"  warn: leaf '{leaf}' returned HTTP {status}; skipping")
                continue
            group_total += len(leaf_products)
            for p in leaf_products:
                pid = str(p["id"])
                memberships.setdefault(pid, set()).add(group)
                extras.setdefault(pid, p)
        print(f"labeled {len([p for p, g in memberships.items() if group in g])} products for group '{group}' ({group_total} leaf hits)")

    current: dict[str, dict[str, Any]] = {}
    for src in (universal, list(extras.values())):
        for p in src:
            pid = str(p["id"])
            if pid in current:
                continue
            price = product_price_range(p)
            current[pid] = {
                "id": pid,
                "title": p.get("title") or "",
                "handle": p.get("handle") or "",
                "vendor": p.get("vendor") or "",
                "available": product_available(p),
                "price_min": price[0] if price else None,
                "price_max": price[1] if price else None,
                "collections": sorted(memberships.get(pid, set())),
            }
    return current


# ---------- rule evaluation ----------

def _ci_contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def _normalize_vendor(vendor: str) -> str:
    """Farm Link Hawaii appends a location suffix like ' (Oʻahu)' to most
    vendors. Strip one trailing parenthesised chunk so a watchlist entry of
    `vendor_equals: Sweet Cane Cafe` matches `Sweet Cane Cafe (Hawaiʻi)`."""
    v = vendor.strip()
    if v.endswith(")") and "(" in v:
        v = v[: v.rfind("(")].rstrip()
    return v.lower()


def _vendor_equals(product_vendor: str, target: str) -> bool:
    return _normalize_vendor(product_vendor) == target.strip().lower()


def matches_any(product: dict[str, Any], conditions: list[dict[str, str]]) -> bool:
    for c in conditions or []:
        if "title_contains" in c and _ci_contains(product["title"], c["title_contains"]):
            return True
        if "vendor_equals" in c and _vendor_equals(product["vendor"], c["vendor_equals"]):
            return True
    return False


def rule_applies_to_collections(rule: dict[str, Any], product_collections: list[str]) -> bool:
    scope = rule.get("collections")
    if not scope:
        return True  # "any" collection
    return bool(set(scope) & set(product_collections))


def evaluate_rules(product: dict[str, Any], rules: list[dict[str, Any]]) -> list[str]:
    matched: list[str] = []
    for rule in rules:
        if not rule_applies_to_collections(rule, product["collections"]):
            continue
        if not matches_any(product, rule.get("match_any", [])):
            continue
        if matches_any(product, rule.get("exclude_if_any", [])):
            continue
        matched.append(rule["name"])
    return matched


# ---------- state ----------

def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"bootstrapped": False, "products": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        print(f"warn: {STATE_PATH} invalid JSON; treating as fresh", file=sys.stderr)
        return {"bootstrapped": False, "products": {}}


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


# ---------- digest ----------

# Discord embed limits: 4096 chars in description, 6000 chars total per
# message, max 10 embeds per message. We chunk well under those.
_EMBED_DESC_BUDGET = 3500
_DIGEST_HAWAII_TZ = timezone(timedelta(hours=-10))  # HST, no DST
COLOR_NEW_TODAY = 0x3498DB        # blue
COLOR_WATCHLIST = 0x2ECC71        # green


def hawaii_today() -> str:
    return datetime.now(_DIGEST_HAWAII_TZ).strftime("%Y-%m-%d")


def _bullet(p: dict[str, Any], rules_label: str | None) -> str:
    price = (
        (float(p["price_min"]), float(p["price_max"]))
        if p.get("price_min") is not None else None
    )
    handle = p["handle"] or ""
    title = p["title"] or handle or "Untitled"
    url = f"{CONFIG['store_base']}/products/{handle}"
    parts = [f"**[{title}]({url})**"]
    if p.get("vendor"):
        parts.append(p["vendor"])
    parts.append(format_price(price))
    if rules_label:
        parts.append(rules_label)
    return "• " + " — ".join(parts)


def _chunk_lines(lines: list[str], budget: int = _EMBED_DESC_BUDGET) -> list[str]:
    """Pack lines into chunks each <= budget chars."""
    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for line in lines:
        n = len(line) + 1  # +1 for the joining newline
        if used + n > budget and buf:
            chunks.append("\n".join(buf))
            buf, used = [], 0
        buf.append(line)
        used += n
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def build_digest_embeds(
    new_today: list[dict[str, Any]],
    in_stock_watchlist: list[dict[str, Any]],
    today: str,
) -> list[dict[str, Any]]:
    embeds: list[dict[str, Any]] = []

    if new_today:
        lines: list[str] = []
        for entry in new_today:
            star = " ⭐" if entry["matched"] else ""
            label = ", ".join(entry["matched"]) if entry["matched"] else None
            lines.append(_bullet(entry["cur"], label) + star)
        for i, chunk in enumerate(_chunk_lines(lines)):
            embeds.append({
                "title": (
                    f"🆕 New today — {today} ({len(new_today)})"
                    if i == 0 else f"🆕 New today (cont. {i + 1})"
                ),
                "color": COLOR_NEW_TODAY,
                "description": chunk,
            })

    if in_stock_watchlist:
        lines = []
        for entry in in_stock_watchlist:
            label = ", ".join(entry["matched"])
            lines.append(_bullet(entry["cur"], label))
        for i, chunk in enumerate(_chunk_lines(lines)):
            embeds.append({
                "title": (
                    f"🥭 Watchlist in stock — {today} ({len(in_stock_watchlist)})"
                    if i == 0 else f"🥭 Watchlist in stock (cont. {i + 1})"
                ),
                "color": COLOR_WATCHLIST,
                "description": chunk,
            })

    return embeds


def post_discord(webhook_url: str, embeds: list[dict[str, Any]]) -> None:
    chunk_size = CONFIG["discord_max_embeds"]
    for i in range(0, len(embeds), chunk_size):
        payload = {
            "username": CONFIG["discord_webhook_username"],
            "embeds": embeds[i:i + chunk_size],
        }
        for attempt in (1, 2):
            r = requests.post(webhook_url, json=payload, timeout=CONFIG["request_timeout"])
            if r.status_code == 429 and attempt == 1:
                retry_after = float(r.headers.get("Retry-After", "2"))
                time.sleep(min(retry_after, 30))
                continue
            if not r.ok:
                raise RuntimeError(f"discord POST failed: {r.status_code} {r.text[:200]}")
            break


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Farm Link Hawaii daily watchlist digest")
    parser.add_argument("--dry-run", action="store_true", help="print digest; don't POST or write state")
    parser.add_argument("--force-bootstrap", action="store_true", help="reset state and re-record everything silently")
    parser.add_argument("--force-digest", action="store_true", help="re-send digest even if already sent today")
    args = parser.parse_args()

    watchlist = yaml.safe_load(WATCHLIST_PATH.read_text()) or {}
    rules: list[dict[str, Any]] = watchlist.get("rules", []) or []

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    today = hawaii_today()
    ts = now_iso()

    session = build_session()
    validate_collection_groups(session)

    current = build_current_products(session)
    print(f"built catalog: {len(current)} unique products")

    state = load_state()

    if args.force_bootstrap or not state.get("bootstrapped"):
        products_state: dict[str, Any] = {}
        for pid, cur in current.items():
            matched = evaluate_rules(cur, rules)
            products_state[pid] = {
                "title": cur["title"],
                "handle": cur["handle"],
                "vendor": cur["vendor"],
                "collections": list(cur["collections"]),
                "first_seen": ts,
                "last_seen": ts,
                "available": cur["available"],
                "matched_rules": list(matched),
            }
        new_state = {
            "bootstrapped": True,
            "last_digest_date": today,
            "last_run_at": ts,
            "products": products_state,
        }
        if args.dry_run:
            print(f"[dry-run] bootstrap would record {len(products_state)} products; not writing state")
        else:
            write_state(new_state)
            print(f"bootstrapped {len(products_state)} products; no digest sent")
        return 0

    prior_products: dict[str, Any] = state.get("products", {}) or {}
    last_digest_date = state.get("last_digest_date")
    already_sent_today = (last_digest_date == today) and not args.force_digest

    # Section A: products that weren't in the prior catalog snapshot.
    new_today: list[dict[str, Any]] = []
    for pid, cur in current.items():
        if pid in prior_products:
            continue
        matched = evaluate_rules(cur, rules)
        new_today.append({"pid": pid, "cur": cur, "matched": matched})

    # Section B: every watchlist match that's currently in stock per the
    # storefront HTML (the bulk products.json `available` flag is stale).
    in_stock_watchlist: list[dict[str, Any]] = []
    confirmed_stock_pids: set[str] = set()
    confirmed_oos_pids: set[str] = set()
    for pid, cur in current.items():
        matched = evaluate_rules(cur, rules)
        if not matched:
            continue
        if not cur["available"]:
            continue  # trust the bulk feed's negative
        if confirm_in_stock(session, cur["handle"]):
            confirmed_stock_pids.add(pid)
            in_stock_watchlist.append({"pid": pid, "cur": cur, "matched": matched})
        else:
            confirmed_oos_pids.add(pid)

    # Sort each section: matched-watchlist first, then alphabetical by title.
    new_today.sort(key=lambda e: (not e["matched"], (e["cur"]["title"] or "").lower()))
    in_stock_watchlist.sort(key=lambda e: (e["cur"]["title"] or "").lower())

    # Build the new state. Use the HTML-confirmed availability when we have it.
    new_products_state: dict[str, Any] = {}
    for pid, cur in current.items():
        prior = prior_products.get(pid)
        matched = evaluate_rules(cur, rules)
        avail = cur["available"]
        if pid in confirmed_oos_pids:
            avail = False
        elif pid in confirmed_stock_pids:
            avail = True
        new_products_state[pid] = {
            "title": cur["title"],
            "handle": cur["handle"],
            "vendor": cur["vendor"],
            "collections": list(cur["collections"]),
            "first_seen": (prior.get("first_seen") if prior else None) or ts,
            "last_seen": ts,
            "available": avail,
            "matched_rules": list(matched),
        }
    # Carry over products that fell out of the fetch (mark unavailable).
    for pid, prior in prior_products.items():
        if pid in new_products_state:
            continue
        carried = dict(prior)
        carried["available"] = False
        carried["last_seen"] = prior.get("last_seen") or ts
        new_products_state[pid] = carried

    print(
        f"new today: {len(new_today)}, "
        f"watchlist in stock: {len(in_stock_watchlist)} "
        f"(confirmed OOS on {len(confirmed_oos_pids)} candidate(s))"
    )

    embeds = build_digest_embeds(new_today, in_stock_watchlist, today)

    if args.dry_run:
        for i, emb in enumerate(embeds, 1):
            print(f"--- embed {i} ---")
            print(json.dumps(emb, indent=2, ensure_ascii=False))
        print("[dry-run] not POSTing, not writing state")
        return 0

    if embeds and already_sent_today:
        print(f"already sent digest for {today}; skipping POST (use --force-digest to override)")
    elif embeds:
        if not webhook_url:
            print("ERROR: DISCORD_WEBHOOK_URL not set but digest is queued", file=sys.stderr)
            return 2
        post_discord(webhook_url, embeds)
        print(f"posted digest with {len(embeds)} embed(s) to Discord")
    else:
        print("nothing to report today")

    write_state({
        "bootstrapped": True,
        "last_digest_date": today if (embeds and not already_sent_today) else last_digest_date,
        "last_run_at": ts,
        "products": new_products_state,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
