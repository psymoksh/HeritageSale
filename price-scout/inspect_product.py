"""Why does one Shopify product show no sizes or measurements?   python3 inspect_product.py PRODUCT_URL
Fetches the product's public JSON and page, then prints what the app sees: option names, size buttons, category,
and the size chart rows it can match to a size. Writes nothing."""
import re, sys
import requests
import app


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = sys.argv[1].split("?")[0].rstrip("/")
    j = requests.get(url + ".js", headers=app.HEADERS, timeout=20).json()
    opts = [o["name"] if isinstance(o, dict) else o for o in j.get("options", [])]
    p = {"title": j["title"], "vendor": j.get("vendor"), "product_type": j.get("type") or j.get("product_type", ""),
         "tags": j.get("tags", []), "body_html": j.get("description") or j.get("body_html"),
         "options": [{"name": n} for n in opts],
         "variants": [{"option1": v.get("option1"), "option2": v.get("option2"), "option3": v.get("option3"),
                       "available": v.get("available"), "title": v.get("title")} for v in j["variants"]]}
    print("Title:       ", p["title"])
    print("Options:     ", opts)
    print("Variants:    ", [(v["title"], v["available"]) for v in p["variants"]])
    print("Category:    ", app.categorize(p["product_type"], p["title"], p["tags"]), f'(product_type={p["product_type"]!r})')
    print("Size buttons:", app.variant_sizes(p) or "NONE: no option is named Size/サイズ (and it is not the only option)")
    desc = app.parse_charts(p["body_html"])
    print("Chart in description:", list(desc) or "none")
    html = requests.get(url, headers=app.HEADERS, timeout=20).text
    page = app.parse_charts(html)
    print("Chart on page:       ", list(page) or "none")
    rows = app.measure_rows(p, page)
    print("Rows matched to a size button:", [(r["size"], {k: v for k, v in r.items() if k not in ("size", "in_stock")}) for r in rows] or "NONE")
    if page and not rows:
        print("Chart sizes", list(page), "do not match button sizes", [s for s, _ in app.variant_sizes(p)])


main()
