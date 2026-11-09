"""Why does one Shopify product show no sizes or measurements?   python3 inspect_product.py PRODUCT_URL
Fetches the product's public JSON and page, then prints what the app sees: option names, size buttons, category,
and the size chart rows it can match to a size. Writes nothing."""
import re, sys
import requests
import app


def fetch_product(url):
    """Product data from <url>.js (has availability); falls back to <url>.json (no availability) if the store blocks .js."""
    tried = []
    for suffix in (".js", ".json"):
        r = requests.get(url + suffix, headers=app.HEADERS, timeout=20, allow_redirects=True)
        try:
            j = r.json()
        except ValueError:
            tried.append(f"{url + suffix} -> HTTP {r.status_code}, {r.headers.get('content-type', '?')}, starts: {r.text[:120]!r}")
            continue
        if suffix == ".json":
            j = j["product"]
            j["description"] = j.get("body_html")
            j["type"] = j.get("product_type")
            for v in j["variants"]:
                v.setdefault("available", True)    # .json does not say; treat every size as in stock
            print("NOTE: this store did not return .js, so stock is unknown (all sizes shown as in stock).")
        return j
    sys.exit("Could not read product data. Check the URL is a full product page (https://store.com/products/handle).\n" + "\n".join(tried))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = sys.argv[1].split("?")[0].rstrip("/")
    j = fetch_product(url)
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
    base = "/".join(url.split("/")[:3])
    try:
        meta = requests.get(base + "/meta.json", headers=app.HEADERS, timeout=15).json()
    except Exception:
        meta = {}
    try:
        pj = requests.get(url + ".json", headers=app.HEADERS, timeout=15).json()["product"]["variants"]
        cat_prices = [v["price"] for v in pj]
    except Exception:
        cat_prices = "unavailable"
    print("Store currency (/meta.json):", meta.get("currency"), "| country:", meta.get("country"))
    print("Price in catalog (.json):   ", cat_prices, "(this is what the app uses; the cheapest in-stock variant)")
    print("Price in product JS (.js):  ", [round(v["price"] / 100, 2) if isinstance(v.get("price"), (int, float)) else v.get("price") for v in j["variants"]], "(cents / 100)")
    desc = app.parse_charts(p["body_html"])
    print("Chart in description:", list(desc) or "none")
    html = requests.get(url, headers=app.HEADERS, timeout=20).text
    ld = re.findall(r'"priceCurrency"\s*:\s*"([A-Z]{3})"[^}]*?"price"\s*:\s*"?([\d.,]+)|"price"\s*:\s*"?([\d.,]+)"?[^}]*?"priceCurrency"\s*:\s*"([A-Z]{3})', html)
    print("Price shown to visitors (page data):", ld[:4] or "not found in page")
    page = app.parse_charts(html)
    print("Chart on page:       ", list(page) or "none")
    rows = app.measure_rows(p, page)
    print("Rows matched to a size button:", [(r["size"], {k: v for k, v in r.items() if k not in ("size", "in_stock")}) for r in rows] or "NONE")
    if page and not rows:
        print("Chart sizes", list(page), "do not match button sizes", [s for s, _ in app.variant_sizes(p)])


main()
