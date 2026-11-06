#!/usr/bin/env python3
"""Why does a store return few or no results for a search? Run on a normal internet connection:

    python3 explain_search.py "momotaro 0705" -s "Red Cast"

Loads that store's catalog and shows, for each word you typed, how many products contain it as a whole word,
how many match every word (what the site shows), and example products that match all but one word, with the
word that was missing. Words that appear only inside longer words ('selvage' in 'selvages') show as near misses.
"""
import argparse, re
import app


def words_of(p, base):
    vendor = p.get("vendor") or ""
    tags = p.get("tags") or []
    tags = tags.split(",") if isinstance(tags, str) else tags
    text = f'{vendor}: {p["title"]} {p.get("product_type", "")} {" ".join(tags)}'
    return text, app._plain(text).split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-s", "--store", required=True, help="store name (part of it is enough)")
    a = ap.parse_args()
    site = next((s for s in app.load_sites() if a.store.lower() in s["name"].lower()), None)
    if not site:
        raise SystemExit("no store with that name in sites.json")
    base = site["base_url"].rstrip("/")
    products = app.get_catalog(base)
    if not products:
        raise SystemExit(f"{site['name']}: no Shopify catalog (html/square store); use check_stores.py instead")
    typed = app._plain(a.query).split()
    rows = [(p, *words_of(p, base)) for p in products]
    print(f"{site['name']}: {len(products)} products. You typed: {typed}\n")
    for x in typed:
        whole = sum(any(w == x or (any(c.isdigit() for c in x) and w.startswith(x)) for w in ws) for _, _, ws in rows)
        part = sum(x in " ".join(ws) for _, _, ws in rows)
        print(f"  '{x}': {whole} products contain it as a word" + (f" ({part - whole} more only inside longer words)" if part > whole else ""))
    hit = [r for r in rows if app.matches(r[1], a.query)]
    print(f"\nProducts matching every word (what the site shows): {len(hit)}")
    for p, text, _ in hit[:5]:
        print("   ", p["title"])
    if len(hit) < 5:
        print("\nClose misses (all words but one):")
        shown = 0
        for p, text, ws in rows:
            miss = [x for x in typed if not app.matches(text, x)]
            if len(typed) > 1 and len(miss) == 1 and shown < 8:
                print(f"    {p['title']}   <- missing '{miss[0]}'")
                shown += 1
        if not shown:
            print("    none")


if __name__ == "__main__":
    main()
