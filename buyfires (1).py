#!/usr/bin/env python3
"""
BuyFires daily social poster — helper script.
Consolidates the token-heavy parts of the workflow (WooCommerce paging/filtering,
cooldown-rule application, Pinterest board mapping) into local computation so the
LLM only ever sees compact JSON, not raw product/post dumps.

Subcommands:
  build-catalog <page1.json> <page2.json> <page3.json> -o catalog.json
      Parse raw WC product pages into a compact catalog (eligible + full, for
      permalink/range lookups of historically-posted products that may no
      longer be in-stock). Requires the WooCommerce fetch's _fields to include
      regular_price and sale_price (not just price) so the caption step can
      tell whether a product is genuinely on sale.

  match-history <catalog.json> <hints.json> -o ledger.json
      Resolve a list of {date, name_hint} historical-post hints to
      {date, permalink, product_range, matched_name} by fuzzy name match
      against the catalog. Unresolved hints are reported, not silently dropped.

  pick <catalog.json> <ledger.json> --date YYYY-MM-DD [--seed N]
      Apply the 60-day permalink cooldown and 7-day product-range cooldown
      (relative to --date), randomly select one eligible candidate, append it
      to the ledger (so the next `pick` call for a later date sees it), and
      print the chosen product + its Pinterest board name as JSON.

Board mapping (category name -> Pinterest board):
  Media Wall Fires                              -> Mediawall Fires
  Electric Stoves                               -> Electric Stoves
  Wood Burning Stoves / Multi-Fuel Stoves        -> Woodburning/Multifuel Stoves
  Gas Fires / Gas Stoves                         -> Gas Fires
  everything else                                -> BuyFires Online
"""
import json
import random
import re
import sys
import argparse
from datetime import date as Date, timedelta
from urllib.parse import urlsplit


def base_url(permalink):
    """Strip query string (e.g. ?attribute_pa_flue-type=...) so different
    attribute-variant URLs of the same variable product compare as equal."""
    s = urlsplit(permalink)
    return f"{s.scheme}://{s.netloc}{s.path}"

BOARD_MAP = {
    "media wall fires": "Mediawall Fires",
    "electric stoves": "Electric Stoves",
    "wood burning stoves": "Woodburning/Multifuel Stoves",
    "multi-fuel stoves": "Woodburning/Multifuel Stoves",
    "gas fires": "Gas Fires",
    "gas stoves": "Gas Fires",
}
DEFAULT_BOARD = "BuyFires Online"


def board_for_categories(cat_names):
    for c in cat_names:
        b = BOARD_MAP.get(c.strip().lower())
        if b:
            return b
    return DEFAULT_BOARD


def get_range(product):
    for a in product.get("attributes") or []:
        if a.get("slug") == "pa_product-range":
            opts = a.get("options") or []
            if opts:
                return opts[0]
    return None


def compact_product(p):
    images = [
        {"src": im.get("src"), "alt": im.get("alt") or ""}
        for im in (p.get("images") or [])
    ][:4]
    cats = [c["name"] for c in (p.get("categories") or [])]
    return {
        "id": p["id"],
        "name": p["name"],
        "permalink": p["permalink"],
        "price": p.get("price"),
        "regular_price": p.get("regular_price"),
        "sale_price": p.get("sale_price"),
        "on_sale": bool(p.get("on_sale")) or bool(
            p.get("regular_price") and p.get("sale_price")
            and p.get("regular_price") != p.get("sale_price")
        ),
        "stock_status": p.get("stock_status"),
        "status": p.get("status"),
        "product_range": get_range(p),
        "categories": cats,
        "board": board_for_categories(cats),
        "images": images,
    }


def cmd_build_catalog(args):
    all_products = []
    for f in args.pages:
        all_products.extend(json.load(open(f)))
    full = [compact_product(p) for p in all_products]
    eligible = [
        p for p in full
        if p["status"] == "publish" and p["stock_status"] == "instock"
        and p["price"] not in (None, "")
    ]
    out = {"full": full, "eligible": eligible}
    json.dump(out, open(args.output, "w"))
    print(json.dumps({
        "total_products": len(full),
        "eligible": len(eligible),
        "output": args.output,
    }))


def cmd_match_history(args):
    catalog = _load_json(args.catalog, "catalog")
    full = catalog["full"]
    hints = _load_json(args.hints, "hints")
    ledger = []
    unresolved = []
    for h in hints:
        hint = h["name_hint"].lower()
        # exact-ish substring match first, else token-overlap score
        best, best_score = None, 0
        for p in full:
            name = p["name"].lower()
            if hint in name or name in hint:
                best = p
                best_score = 999
                break
            tokens_h = set(re.findall(r"[a-z0-9]+", hint))
            tokens_n = set(re.findall(r"[a-z0-9]+", name))
            score = len(tokens_h & tokens_n)
            if score > best_score:
                best, best_score = p, score
        if best and best_score >= 2:
            ledger.append({
                "date": h["date"],
                "permalink": best["permalink"],
                "product_range": best["product_range"],
                "matched_name": best["name"],
                "hint": h["name_hint"],
            })
        else:
            unresolved.append(h)
    json.dump(ledger, open(args.output, "w"), separators=(",", ":"))
    print(json.dumps({"resolved": len(ledger), "unresolved": unresolved, "output": args.output}))


def _load_json(path, what):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        print(json.dumps({"error": f"{what} not found: {path}"}))
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"{what} is not valid JSON ({path}): {e}"}))
        sys.exit(1)


def cmd_pick(args):
    catalog = _load_json(args.catalog, "catalog")
    ledger = _load_json(args.ledger, "ledger")
    target = Date.fromisoformat(args.date)
    cutoff60 = target - timedelta(days=60)
    cutoff7 = target - timedelta(days=7)

    excl_permalinks = set()
    excl_ranges = set()
    for entry in ledger:
        d = Date.fromisoformat(entry["date"])
        if cutoff60 <= d < target:
            excl_permalinks.add(base_url(entry["permalink"]))
        if cutoff7 <= d < target and entry.get("product_range"):
            excl_ranges.add(entry["product_range"])

    candidates = [
        p for p in catalog["eligible"]
        if base_url(p["permalink"]) not in excl_permalinks
        and p.get("product_range") not in excl_ranges
    ]
    fallback_used = False
    if not candidates:
        # fallback: ignore only the range rule
        candidates = [p for p in catalog["eligible"] if base_url(p["permalink"]) not in excl_permalinks]
        fallback_used = True
        if not candidates:
            print(json.dumps({"error": "no eligible candidates even after fallback"}))
            sys.exit(1)

    if args.seed is not None:
        random.seed(args.seed)
    chosen = random.choice(candidates)

    ledger.append({
        "date": args.date,
        "permalink": chosen["permalink"],
        "product_range": chosen.get("product_range"),
        "matched_name": chosen["name"],
        "hint": "picked-by-script",
    })
    json.dump(ledger, open(args.ledger, "w"), separators=(",", ":"))

    print(json.dumps({
        "date": args.date,
        "fallback_used": fallback_used,
        "candidate_count_before_pick": len(candidates),
        "chosen": chosen,
    }, separators=(",", ":")))


PERMALINK_RE = re.compile(r"https://buyfires\.co\.uk/product/[a-zA-Z0-9\-]+/(?:\?[^\s\"]*)?")


def cmd_build_ledger(args):
    """Auto-build ledger entries from raw getScheduledPosts JSON dumps (one file
    per date-range chunk, each the raw {"data":[...]} MCP result). Two signals,
    in order of confidence:
      1. An explicit buyfires.co.uk/product/... URL in the post text.
      2. instagramData.carouselProductTags[].productName (Metricool's own
         product tagging) matched against the catalog by name.
    Posts matched by neither are reported as unresolved for manual review /
    hint-based resolution (see match-history), rather than silently dropped.
    """
    catalog = _load_json(args.catalog, "catalog")
    full = catalog["full"]
    by_base_url = {base_url(p["permalink"]): p for p in full}

    def find_by_name(name_guess):
        name_guess = name_guess.lower()
        for p in full:
            if name_guess in p["name"].lower() or p["name"].lower() in name_guess:
                return p
        return None

    ledger, unresolved, seen = [], [], set()
    for path in args.posts:
        for post in _load_json(path, "posts")["data"]:
            date_str = post["publicationDate"]["dateTime"][:10]
            key = (date_str, post.get("uuid"))
            if key in seen:
                continue
            seen.add(key)

            m = PERMALINK_RE.search(post.get("text", ""))
            product = by_base_url.get(base_url(m.group(0))) if m else None

            if not product:
                for tags in (post.get("instagramData", {}).get("carouselProductTags") or {}).values():
                    for t in tags:
                        product = find_by_name(t.get("productName", ""))
                        if product:
                            break
                    if product:
                        break

            if product:
                ledger.append({
                    "date": date_str,
                    "permalink": product["permalink"],
                    "product_range": product["product_range"],
                    "matched_name": product["name"],
                    "hint": "auto-extracted",
                })
            else:
                unresolved.append({"date": date_str, "text_snippet": post.get("text", "")[:120]})

    json.dump(ledger, open(args.output, "w"), separators=(",", ":"))
    print(json.dumps({"resolved": len(ledger), "unresolved": unresolved, "output": args.output}))


def cmd_best_times(args):
    """Given getBestTimeToPostByNetwork raw JSON dumps (one file per network) and
    a list of target dates, output the best hour per date per network, plus the
    fixed Pinterest fallback (20:00 daily, 09:00 on Saturday)."""
    networks = {"facebook": args.facebook, "instagram": args.instagram, "linkedin": args.linkedin}
    by_network_dow = {}
    for net, path in networks.items():
        raw = json.load(open(path))
        dow_best = {}
        for day in raw["data"]:
            best = max(day["bestTimesByHour"], key=lambda h: h["value"])
            dow_best[day["dayOfWeek"]] = best["hourOfDay"]
        by_network_dow[net] = dow_best

    schedule = {}
    for date_str in args.dates:
        d = Date.fromisoformat(date_str)
        iso_dow = d.isoweekday()  # 1=Monday ... 7=Sunday, matches API's dayOfWeek
        entry = {net: by_network_dow[net][iso_dow] for net in networks}
        entry["pinterest"] = 9 if iso_dow == 6 else 20  # Saturday=6 -> 09:00, else 20:00
        schedule[date_str] = entry
    print(json.dumps(schedule, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("build-catalog")
    p1.add_argument("pages", nargs="+")
    p1.add_argument("-o", "--output", required=True)
    p1.set_defaults(func=cmd_build_catalog)

    p2 = sub.add_parser("match-history")
    p2.add_argument("catalog")
    p2.add_argument("hints")
    p2.add_argument("-o", "--output", required=True)
    p2.set_defaults(func=cmd_match_history)

    p3 = sub.add_parser("pick")
    p3.add_argument("catalog")
    p3.add_argument("ledger")
    p3.add_argument("--date", required=True)
    p3.add_argument("--seed", type=int, default=None)
    p3.set_defaults(func=cmd_pick)

    p3b = sub.add_parser("build-ledger")
    p3b.add_argument("catalog")
    p3b.add_argument("posts", nargs="+", help="raw getScheduledPosts JSON dump file(s)")
    p3b.add_argument("-o", "--output", required=True)
    p3b.set_defaults(func=cmd_build_ledger)

    p4 = sub.add_parser("best-times")
    p4.add_argument("--facebook", required=True)
    p4.add_argument("--instagram", required=True)
    p4.add_argument("--linkedin", required=True)
    p4.add_argument("--dates", nargs="+", required=True)
    p4.set_defaults(func=cmd_best_times)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
