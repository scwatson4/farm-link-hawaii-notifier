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
import sys
import time
from datetime import datetime, timezone
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
    "color_watchlist": 0x2ECC71,
    "color_new_in_produce": 0x3498DB,
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


def record_product(state_products: dict[str, Any], cur: dict[str, Any], matched_rules: list[str], *, first: bool, notified_first: bool) -> None:
    ts = now_iso()
    state_products[cur["id"]] = {
        "title": cur["title"],
        "handle": cur["handle"],
        "vendor": cur["vendor"],
        "collections": list(cur["collections"]),
        "first_seen": ts,
        "last_seen": ts,
        "available": cur["available"],
        "matched_rules": list(matched_rules),
        "notified_first": notified_first,
        "notified_back_in_stock_at": None,
    }


# ---------- Discord ----------

def build_embed(cur: dict[str, Any], matched_rules: list[str], reason: str, *, new_in_produce: bool) -> dict[str, Any]:
    price = None
    if cur.get("price_min") is not None:
        price = (float(cur["price_min"]), float(cur["price_max"]))

    fields_text: list[str] = []
    if cur["vendor"]:
        fields_text.append(f"Vendor: {cur['vendor']}")
    fields_text.append(f"Price: {format_price(price)}")
    fields_text.append(f"Availability: {'In stock' if cur['available'] else 'Out of stock'}")
    if cur["collections"]:
        fields_text.append(f"Collections: {', '.join(cur['collections'])}")
    if matched_rules:
        fields_text.append(f"Matched: {', '.join(matched_rules)}")
    fields_text.append(f"Reason: {reason}")

    only_new_in_produce = new_in_produce and not matched_rules
    color = CONFIG["color_new_in_produce"] if only_new_in_produce else CONFIG["color_watchlist"]

    return {
        "title": cur["title"] or cur["handle"] or f"Product {cur['id']}",
        "url": f"{CONFIG['store_base']}/products/{cur['handle']}",
        "color": color,
        "description": "\n".join(f"• {line}" for line in fields_text),
    }


def post_discord(webhook_url: str, embeds: list[dict[str, Any]]) -> None:
    payload = {
        "username": CONFIG["discord_webhook_username"],
        "embeds": embeds,
    }
    for attempt in (1, 2):
        r = requests.post(webhook_url, json=payload, timeout=CONFIG["request_timeout"])
        if r.status_code == 429 and attempt == 1:
            retry_after = float(r.headers.get("Retry-After", "2"))
            time.sleep(min(retry_after, 30))
            continue
        if not r.ok:
            raise RuntimeError(f"discord POST failed: {r.status_code} {r.text[:200]}")
        return


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Farm Link Hawaii watchlist notifier")
    parser.add_argument("--dry-run", action="store_true", help="print notifications; don't POST or write state")
    parser.add_argument("--force-bootstrap", action="store_true", help="reset state and re-record everything silently")
    args = parser.parse_args()

    watchlist = yaml.safe_load(WATCHLIST_PATH.read_text()) or {}
    rules: list[dict[str, Any]] = watchlist.get("rules", []) or []
    new_in_produce_enabled = bool((watchlist.get("new_in_produce") or {}).get("enabled"))

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    session = build_session()
    validate_collection_groups(session)

    current = build_current_products(session)
    print(f"built catalog: {len(current)} unique products")

    state = load_state()
    if args.force_bootstrap or not state.get("bootstrapped"):
        products_state: dict[str, Any] = {}
        for pid, cur in current.items():
            matched = evaluate_rules(cur, rules)
            record_product(products_state, cur, matched, first=True, notified_first=True)
        new_state = {"bootstrapped": True, "products": products_state}
        if args.dry_run:
            print(f"[dry-run] bootstrap would record {len(products_state)} products; not writing state")
        else:
            write_state(new_state)
            print(f"bootstrapped {len(products_state)} products; no notifications sent")
        return 0

    prior_products: dict[str, Any] = state.get("products", {}) or {}
    notifications: list[dict[str, Any]] = []  # list of (embed, product_id, updates)
    new_products_state: dict[str, Any] = {}

    for pid, cur in current.items():
        matched = evaluate_rules(cur, rules)
        triggers_new_in_produce = (
            new_in_produce_enabled and "produce" in cur["collections"] and pid not in prior_products
        )

        prior = prior_products.get(pid)
        if prior is None:
            # New product.
            notified_first = False
            if matched or triggers_new_in_produce:
                reason_parts: list[str] = []
                if matched:
                    reason_parts.append("First seen")
                if triggers_new_in_produce and not matched:
                    reason_parts.append("New in produce")
                embed = build_embed(
                    cur, matched, " + ".join(reason_parts) or "First seen",
                    new_in_produce=triggers_new_in_produce,
                )
                notifications.append(embed)
                notified_first = True
            record_product(new_products_state, cur, matched, first=True, notified_first=notified_first)
        else:
            ts = now_iso()
            was_available = bool(prior.get("available"))
            notified_back = prior.get("notified_back_in_stock_at")
            new_record = {
                "title": cur["title"],
                "handle": cur["handle"],
                "vendor": cur["vendor"],
                "collections": list(cur["collections"]),
                "first_seen": prior.get("first_seen") or ts,
                "last_seen": ts,
                "available": cur["available"],
                "matched_rules": list(matched),
                "notified_first": bool(prior.get("notified_first")),
                "notified_back_in_stock_at": notified_back,
            }
            if matched and not was_available and cur["available"] and not notified_back:
                embed = build_embed(cur, matched, "Back in stock", new_in_produce=False)
                notifications.append(embed)
                new_record["notified_back_in_stock_at"] = ts
            if not cur["available"]:
                new_record["notified_back_in_stock_at"] = None
            new_products_state[pid] = new_record

    # Products that vanished from the fetch: keep them, mark unavailable, re-arm restock.
    for pid, prior in prior_products.items():
        if pid in new_products_state:
            continue
        carried = dict(prior)
        carried["available"] = False
        carried["notified_back_in_stock_at"] = None
        carried["last_seen"] = prior.get("last_seen") or now_iso()
        new_products_state[pid] = carried

    print(f"notifications queued: {len(notifications)}")

    if args.dry_run:
        for i, emb in enumerate(notifications, 1):
            print(f"--- notification {i} ---")
            print(json.dumps(emb, indent=2))
        print("[dry-run] not POSTing, not writing state")
        return 0

    if notifications:
        if not webhook_url:
            print("ERROR: DISCORD_WEBHOOK_URL not set but notifications are queued", file=sys.stderr)
            return 2
        chunk = CONFIG["discord_max_embeds"]
        for i in range(0, len(notifications), chunk):
            post_discord(webhook_url, notifications[i:i + chunk])
        print(f"posted {len(notifications)} embeds to Discord")

    write_state({"bootstrapped": True, "products": new_products_state})
    return 0


if __name__ == "__main__":
    sys.exit(main())
