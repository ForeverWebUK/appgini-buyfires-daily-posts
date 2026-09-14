# BuyFires social auto-poster — helper script

`buyfires.py` handles the token-heavy, mechanical parts of BuyFires'
weekly social scheduling run so the LLM only ever reasons over compact
JSON, not raw multi-hundred-KB WooCommerce/Metricool dumps.

It does **not** replace the LLM step entirely — writing captions, judging
which historical posts a product-name hint refers to, and deciding on
best-time-to-post all still need a model in the loop. What it removes is
the token cost of paging/filtering/deduping that data by hand each run.

## Subcommands

```
buyfires.py build-catalog page1.json page2.json page3.json -o catalog.json
```
Parse raw WooCommerce `/wp-json/wc/v3/products` pages (fetched via curl,
`per_page=100`, paginated per `X-WP-TotalPages`) into a compact catalog:
`full` (all products, for looking up historically-posted items that may
no longer be in stock) and `eligible` (status=publish, stock_status=instock,
price set — the actual candidate pool).

```
buyfires.py build-ledger catalog.json posts1.json posts2.json ... -o ledger.json
```
Auto-extract cooldown-ledger entries from raw `getScheduledPosts` MCP
results (fetch in ~2-week date chunks covering the last 60 days, save
each raw `{"data":[...]}` response to a file, pass them all here).
Resolves a post to a product via, in order: (1) an explicit
`buyfires.co.uk/product/...` URL in the post text, (2) Instagram's
`carouselProductTags` product names matched against the catalog.

**Known limitation:** older-style posts that link via a shortened
`f.mtr.cool` URL with no product tags resolve neither way. In testing
against real history this left roughly 3 in 4 posts unresolved for one
batch of older posts (recent posts, which always embed the full
permalink, resolve cleanly). Whoever runs this weekly needs to review
the `unresolved` list the command prints and add manually-judged entries
via `match-history` (below) for anything that matters for this week's
cooldown — read the `text_snippet` for each unresolved post, identify
the product by name, and add a `{"date", "name_hint"}` entry to a
hints file.

```
buyfires.py match-history catalog.json hints.json -o ledger.json
```
Resolve `[{"date": "...", "name_hint": "..."}, ...]` hints (typically
the `unresolved` output from `build-ledger`, with product names filled
in by hand) to full ledger entries by fuzzy name match against the
catalog. Run this *after* `build-ledger` and merge the two ledgers
(concatenate the JSON arrays) before `pick`.

```
buyfires.py pick catalog.json ledger.json --date YYYY-MM-DD
```
Apply the 60-day permalink cooldown and 7-day product-range cooldown
(relative to `--date`), pick one random eligible candidate, append it to
the ledger (so a later `pick` call for a later date in the same batch
sees it too), print the chosen product + Pinterest board name.

Permalinks are compared with their query string stripped (`base_url()`)
before matching — WooCommerce gives variable products different URLs per
attribute selection (e.g. `?attribute_pa_flue-type=balanced-flue` vs
`...=conventional-flue`), and without normalizing this, the same
physical product can slip past the cooldown as if it were new. Caught
and fixed during the first live 7-day run (2026-09-14).

```
buyfires.py best-times --facebook f.json --instagram i.json --linkedin l.json --dates 2026-09-14 ...
```
Given `getBestTimeToPostByNetwork` raw dumps for facebook/instagram/
linkedin (one MCP call per network, `fromDate`/`toDate` spanning the
whole week being scheduled — the tool's own guidance caps a useful
range at ~1 week), compute the best hour per date per network. Pinterest
isn't covered by that tool; the fixed fallback (20:00 daily, 09:00
Saturday) is applied automatically.

## Weekly run outline

1. `build-catalog` from a fresh WooCommerce fetch (3 curl calls,
   `--max-time 15`, retry once on timeout then treat as empty).
2. Fetch `getScheduledPosts` in 2-week chunks covering the last 60 days,
   save each raw response to a file, `build-ledger` from all of them.
3. Read the `unresolved` list; for any post in the last 60 days that
   plausibly still matters for this week's picks, resolve it by hand
   into a hints file and run `match-history`, then merge into the
   ledger.
4. `getBestTimeToPostByNetwork` once per network (facebook/instagram/
   linkedin) for the coming Mon–Sun, `best-times` to get the whole
   week's schedule in one pass.
5. For each of the 7 target dates: `pick`, pull full product attributes
   from the cached catalog pages for caption-writing, write the caption
   (style guide unchanged from the daily version — see prior session),
   `createScheduledPost` ×4 (facebook/instagram/linkedin/pinterest).

## Scheduling this weekly

This script is invoked from an LLM session, not standalone (it needs
live Metricool/WooCommerce credentials and MCP tools only available in
that context). The recurring trigger that runs the session needs to be
reconfigured — from outside this repo, via the Claude Code on the web
UI (Settings → Triggers) — to:

- **Cadence:** every Monday, 05:00 Europe/London.
- **Prompt:** schedule 7 days (Monday–Sunday of the coming week) in one
  run instead of 1 day, using this script. See `WEEKLY_PROMPT.md` for
  the exact prompt text.

The in-session `CronCreate` tool cannot do this: its jobs are
session-only (gone when the session ends) and auto-expire after 7 days
regardless, so it cannot stand in for a durable weekly trigger.
