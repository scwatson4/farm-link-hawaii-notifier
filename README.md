# Farm Link Hawaii daily watchlist digest

Polls the [Farm Link Hawaii](https://farmlinkhawaii.com) Shopify catalog once a
day via GitHub Actions and posts a single Discord message with two sections:

- 🆕 **New today** — every product added to the catalog since yesterday's run,
  in any collection. Watchlist matches get a ⭐.
- 🥭 **Watchlist in stock** — every currently-in-stock product matching the
  watchlist, refreshed every day. Stock state is verified against the real
  storefront (Shopify's bulk feed lies — see Caveats).

The cron fires at **17:00 UTC daily = 07:00 Hawaii Standard Time** (Hawaii
doesn't observe DST). Days with no new arrivals and no in-stock watchlist
matches send no message.

## Watchlist

Configured in [`watchlist.yaml`](./watchlist.yaml):

- **Mangoes** — produce, title contains "mango"
- **Lilikoi / passionfruit** — produce, title contains "lilikoi", "passion
  fruit", or "passionfruit"
- **Coconut** — produce, title contains "coconut"
- **Sugarcane** — produce, title contains "sugarcane" or "sugar cane"
- **Sugarcane juice** — drinks, title contains "sugarcane juice" or "cane juice"
- **Pineapples, but not Dole** — produce, title contains "pineapple", excluding
  vendor "Dole" or titles containing "dole"
- **Sweet Cane Cafe drinks** — drinks from vendor "Sweet Cane Cafe", excluding
  any titles containing "cacao nectar"

## How collection labelling works (read this before editing)

Farm Link Hawaii's top-level Shopify smart collections (`produce`, `drinks`,
`fruits`, `vegetables`) return `{"products": []}` from the vanilla Shopify
`/collections/{handle}/products.json` API — their UI renders products
dynamically via a third-party merchandising app. Leaf collections like `mango`,
`juice`, `kombucha`, `berries` work fine.

So `check.py` fetches:

1. `collections/all-products/products.json` as the canonical product universe
   (catches everything, including items in no leaf collection), and
2. A curated list of leaf collections, purely to label each product with
   logical `produce` / `drinks` membership.

Both lists live in the `CONFIG` dict at the top of
[`check.py`](./check.py). Rules in `watchlist.yaml` still reference the logical
names `produce` and `drinks`. If the store renames a leaf, `check.py` will exit
non-zero with a message naming the group with no populated leaves — edit
`CONFIG['collection_groups']` and push.

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

If Actions is disabled, enable it under **Settings → Actions → General → Allow
all actions**.

### 4. Trigger the first run (bootstrap, silent)

```sh
gh workflow run check.yml -R scwatson4/farm-link-hawaii-notifier
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
```

All string comparisons are case-insensitive. A product fires a rule if it
matches ANY condition in `match_any` AND matches NONE in `exclude_if_any`.
Commit + push; the next scheduled run picks it up.

`vendor_equals` normalises away Farm Link Hawaii's trailing location suffix, so
`vendor_equals: Dole` matches the store's actual `"Dole (Oahu)"` and
`vendor_equals: Sweet Cane Cafe` matches `"Sweet Cane Cafe (Hawaiʻi)"`.

## Manual operations

```sh
# trigger a run now (skipped if today's digest already went out)
gh workflow run check.yml -R scwatson4/farm-link-hawaii-notifier

# local dry-run (prints embeds; no POST, no state write)
DISCORD_WEBHOOK_URL=unused python check.py --dry-run

# re-send today's digest (overrides the once-per-day guard)
python check.py --force-digest

# reset and re-record everything silently
python check.py --force-bootstrap
# or delete state/seen.json — the next run bootstraps automatically
```

## Caveats

- GitHub Actions cron is best-effort; expect 5–15 min lag under load.
- Farm Link's bulk `/products.json` feed reports stale `available: true` for
  many sold-out products. Before listing anything in the "Watchlist in stock"
  section, `check.py` re-fetches the product's storefront HTML and verifies
  the `<button name="add">` element isn't `disabled`. If the theme changes and
  that selector stops matching, the watchlist section will silently empty out
  — the run log's `confirmed OOS on N candidate(s)` count is your canary.
- The "New today" section relies on yesterday's `state/seen.json` having been
  committed back by the workflow. If you reset state mid-week, the next run
  silently re-bootstraps and you'll see no "New today" until the day after.
- Products sometimes drop out of the catalog for a pageload; we carry them
  forward as `available: false` rather than deleting them.
