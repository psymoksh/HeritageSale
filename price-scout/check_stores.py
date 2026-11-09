#!/usr/bin/env python3
"""Real-store health check. Run it from a machine with normal internet access:

    python check_stores.py                 # all stores, whole catalog
    python check_stores.py denim           # only listings matching "denim"
    python check_stores.py -s "Self Edge"  # one store (substring of its name)

For each store it reports: reachable?, how it was read (Shopify catalog or HTML scrape), listings found,
currency + ships-from country, and how many listings have sizes, images, categories and (for a sample of
Bottoms) fit measurements. Writes the full detail to store_report.json. Paste the table back to Claude to tune.
"""
import argparse, json, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import app


def check(site, query, category="Bottoms"):
    t0 = time.time()
    base = site["base_url"].rstrip("/")
    row = {"store": site["name"], "url": base, "ok": False}
    if not query and site.get("type") == "html":  # search-page stores can't list a whole catalog
        query = "jeans"
        row["query"] = query
    try:
        name, offers, err = app.search_site(site, query, None)
    except Exception as e:                      # noqa: BLE001 - report, never crash the run
        row["error"] = f"{type(e).__name__}: {e}"[:160]
        return row
    row["seconds"] = round(time.time() - t0, 1)
    if err and not offers:
        row["error"] = err
        try:                                    # say WHY: blocked, DNS, 403, not Shopify...
            r = app.requests.get(base, headers=app.HEADERS, timeout=10)
            row["error"] = f"{err}; home page HTTP {r.status_code}" + ("" if r.ok else " (blocked or down)")
        except Exception as e:                  # noqa: BLE001
            row["error"] = f"{err}; cannot connect ({type(e).__name__})"
        return row
    row["ok"] = True
    row["method"] = site.get("type") or (
        "shopify" if app.shop_meta(base) or any(o.get("_fill") is not None for o in offers) else "html")
    row["listings"] = len(offers)
    row["currency"] = offers[0]["currency"] + ("?" if offers[0].get("assumed") else "")
    row["ships_from"] = app.country_name(offers[0].get("ships_from")) or "unknown"
    row["with_sizes"] = sum(bool(o["sizes"]) for o in offers)
    row["with_images"] = sum(bool(o.get("image")) for o in offers)
    row["with_sale"] = sum(bool(o.get("was")) for o in offers)
    row["categories"] = dict(Counter(o["category"] for o in offers).most_common())
    prices = [o["price"] for o in offers if o["price"]]
    row["price_range"] = [min(prices), max(prices)] if prices else None
    sample = [o for o in offers if o["category"] == category][:10]     # jeans by default; -c Tops checks chest/shoulder/length/sleeve
    if sample:
        app.fill_measures(sample)
        row["sampled"] = len(sample)
        row["with_measures"] = sum(bool(o["measures"]) for o in sample)
        row["measure_category"] = category
    row["samples"] = [f'{o["title"]} | {o["price"]} {o["currency"]} | {o["category"]}' for o in offers[:3]]
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="", help="optional product text; blank = whole catalog")
    ap.add_argument("-s", "--store", help="only stores whose name contains this")
    ap.add_argument("-c", "--category", default="Bottoms", choices=["Bottoms", "Tops", "Outerwear"],
                    help="which garments to sample for measurements (default Bottoms; Tops and Outerwear read chest/shoulder/length/sleeve)")
    a = ap.parse_args()
    sites = [s for s in app.load_sites() if not a.store or a.store.lower() in s["name"].lower()]
    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(lambda s: check(s, a.query, a.category), sites))
    print(f'{"STORE":24} {"OK":3} {"LISTINGS":>8} {"CUR":5} {"SHIPS FROM":14} {"SIZES":>6} {"IMG":>5} {"MEAS":>7}  NOTE')
    for r in rows:
        if r["ok"]:
            meas = f'{r["with_measures"]}/{r["sampled"]}' if "sampled" in r else "-"
            print(f'{r["store"][:24]:24} yes {r["listings"]:>8} {r["currency"]:5} {r["ships_from"][:14]:14} '
                  f'{r["with_sizes"]:>6} {r["with_images"]:>5} {meas:>7}  {r["method"]}')
        else:
            print(f'{r["store"][:24]:24} NO  {"":>8} {"":5} {"":14} {"":>6} {"":>5} {"":>7}  {r.get("error", "")[:70]}')
    good = sum(r["ok"] for r in rows)
    print(f"\n{good}/{len(rows)} stores returned listings. Detail: store_report.json")
    json.dump(rows, open("store_report.json", "w"), indent=2, ensure_ascii=False)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
