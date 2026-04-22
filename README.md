# Farm Link Hawaii watchlist notifier

Polls the [Farm Link Hawaii](https://farmlinkhawaii.com) Shopify catalog every
30 minutes via GitHub Actions and posts Discord notifications when products
matching a curated watchlist appear — or come back in stock.

## What it alerts on

Configured in [`watchlist.yaml`](./watchlist.yaml):

- **Mangoes** — any product whose title contains "mango"
- **Lilikoi / passionfruit** — any product whose title contains "lilikoi",
  "passion fruit", or "passionfruit"
- **Sugarcane in produce** — produce items whose title contains "sugarcane"
  or "sugar cane"
- **Sugarcane juice** — drinks whose title contains "sugarcane juice" or
  "cane juice" (any vendor)
- **Pineapples, but not Dole** — produce with "pineapple" in the title,
  excluding vendor "Dole" or titles containing "dole"
- **Sweet Cane Cafe drinks** — drinks from vendor "Sweet Cane Cafe", except
  anything whose title contains "cacao nectar"
- **Unique fruit alert** — any never-before-seen product appearing in the
  produce collection (the "new fruit dropped" signal)

Each match fires once when the product first appears, and again if a matching
product transitions from out-of-stock → in-stock.

## How collection labelling works (read this before editing)

Farm Link Hawaii's top-level Shopify smart collections (`produce`, `drinks`,
`fruits`, `vegetables`) return `{"products": []}` from the vanilla Shopify
`/collections/{handle}/products.json` API — their UI renders products
dynamically via a third-party merchandising app. Leaf collections like
`mango`, `juice`, `kombucha`, `berries` work fine.

So `check.py` fetches:

1. `collections/all-products/products.json` as the canonical product universe
   (catches everything, including items in no leaf collection), and
2. A curated list of leaf collections, purely to label each product with
   logical `produce` / `drinks` membership.

Both lists live in the `CONFIG` dict at the top of
[`check.py`](./check.py#L24). Rules in `watchlist.yaml` still reference the
logical names `produce` and `drinks`. If the store renames a leaf,
`check.py` will exit non-zero with a message naming the group with no
populated leaves — edit `CONFIG['collection_groups']` and push.

## One-time setup

### 1. Create a Discord webhook (manual)

In Discord: target channel → **Edit Channel** (gear icon) → **Integrations** →
**Webhooks** → **New Webhook** → set a name/avatar → **Copy Webhook URL**.

### 2. Store it as a repo secret

```sh
gh secret set DISCORD_WEBHOOK_URL -R scwatson4/farm-link-hawaii-notifier
# paste the URL when prompted, then press Enter + Ctrl-D
```

### 3. Push the repo and verify Actions

```sh
git push -u origin claude/farm-link-monitor-Ormaf
gh workflow list -R scwatson4/farm-link-hawaii-notifier
```

If Actions is disabled for the repo, enable it under
**Settings → Actions → General → Allow all actions**.

### 4. Trigger the first run (bootstrap, silent)

```sh
gh workflow run check.yml -R scwatson4/farm-link-hawaii-notifier
gh run list  -R scwatson4/farm-link-hawaii-notifier --workflow check.yml --limit 3
gh run watch -R scwatson4/farm-link-hawaii-notifier
```

The first run records the full catalog into `state/seen.json` and sends **no**
Discord messages. Subsequent runs diff against that state.

## Editing the watchlist

[`watchlist.yaml`](./watchlist.yaml) shape:

```yaml
rules:
  - name: "Pretty label"
    collections: [produce, drinks]   # optional; omit for "any collection"
    match_any:                       # OR — any condition can match
      - title_contains: mango        # case-insensitive substring
      - vendor_equals: Sweet Cane Cafe  # case-insensitive, trimmed
    exclude_if_any:                  # OR — any match excludes the product
      - title_contains: dole

new_in_produce:
  enabled: true
```

All string comparisons are case-insensitive. A product fires a rule if it
matches ANY condition in `match_any` AND matches NONE in `exclude_if_any`.
Commit + push; the next scheduled run picks it up.

`vendor_equals` normalises away Farm Link Hawaii's trailing location suffix,
so `vendor_equals: Dole` matches the store's actual `"Dole (Oahu)"` and
`vendor_equals: Sweet Cane Cafe` matches `"Sweet Cane Cafe (Hawaiʻi)"`.

## Manual operations

```sh
# trigger a run now
gh workflow run check.yml -R scwatson4/farm-link-hawaii-notifier

# local dry-run (no POSTs, no state writes)
DISCORD_WEBHOOK_URL=unused python check.py --dry-run

# reset and re-record everything silently
python check.py --force-bootstrap
# or just delete state/seen.json — the next run bootstraps automatically
```

## Caveats

- GitHub Actions cron is best-effort; expect 5–15 min lag under load.
- A product that existed before you added a matching rule will **not** fire —
  dedup keys off product ID, not rule membership. If you want retroactive
  alerts, remove matching entries from `state/seen.json` and push.
- Products sometimes drop out of the catalog for a pageload; we carry them
  forward as `available: false` rather than deleting them, so restocks still
  re-arm cleanly.
