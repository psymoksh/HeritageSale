#!/usr/bin/env python3
"""Why are some stores' fit measurements not being read? Run on a normal internet connection:

    python3 diagnose_fit.py                  # every store that had no readable charts
    python3 diagnose_fit.py -s "Tendrel"     # one store

For a few in-stock jeans per store it checks the catalog description and the product page, says whether
a size chart is there and whether the parser read it, and saves the evidence to fit_diagnosis.txt.
Attach that file to the chat so the parser can be taught each store's chart format.
"""
import argparse, re, sys
from bs4 import BeautifulSoup
import app

WORDS = re.compile(r"waist|thigh|front rise|back rise|rise|inseam|leg opening|knee|hem", re.I)


def snippet(text, n=260):
    m = WORDS.search(text)
    return re.sub(r"\s+", " ", text[max(0, m.start() - 60):m.start() + n]) if m else ""


def inspect(html):
    soup = BeautifulSoup(html or "", "html.parser")
    txt = soup.get_text(" ")
    return {
        "chars": len(html or ""), "tables": len(soup.find_all("table")), "words": bool(WORDS.search(txt)),
        "parsed": app.parse_charts(html or ""), "snippet": snippet(txt),
        "images": [(i.get("alt") or i.get("src") or "")[:80] for i in soup.find_all("img")
                   if re.search(r"size|chart|measure|spec", (i.get("alt", "") + i.get("src", "")), re.I)][:3],
        "iframes": len(soup.find_all("iframe")),
    }


def verdict(cat, page):
    if cat["parsed"] or page["parsed"]:
        return "OK, charts read"
    if cat["words"] or page["words"]:
        return "MEASUREMENT WORDS PRESENT BUT PARSER MISSED IT (parser needs teaching this layout)"
    if cat["images"] or page["images"]:
        return "chart appears to be an image (needs OCR or manual entry)"
    if page["iframes"]:
        return "page uses an embedded widget/iframe for sizing (likely loaded by script)"
    return "no measurement words in the description or the page (chart may be loaded by script, or the store does not publish one)"


def run(site, out):
    base = site["base_url"].rstrip("/")
    out.write(f"\n{'=' * 78}\n{site['name']}  {base}\n")
    try:
        products = app.get_catalog(base) if site.get("type") not in ("html", "square") else None
    except Exception as e:
        products = None
        out.write(f"  catalog error: {e}\n")
    if not products:
        out.write("  (not a Shopify catalog: html/square store; fit data comes from product pages)\n")
        return "skipped (not Shopify)"
    jeans = [p for p in products if app.categorize(p.get("product_type", ""), p["title"], p.get("tags") or []) == "Bottoms"
             and any(v.get("available") for v in p.get("variants", []))][:4]
    verdicts = []
    for p in jeans:
        url = f'{base}/products/{p["handle"]}'
        cat = inspect(p.get("body_html"))
        page = {"parsed": {}, "words": False, "images": [], "iframes": 0, "snippet": "", "chars": 0, "tables": 0}
        try:
            if app.allowed(url):
                page = inspect(app.requests.get(url, headers=app.HEADERS, timeout=15).text)
            else:
                out.write("  robots.txt disallows the product page\n")
        except Exception as e:
            out.write(f"  page fetch failed: {e}\n")
        v = verdict(cat, page)
        verdicts.append(v)
        out.write(f"\n  {p['title']}\n  {url}\n  size options: {[o.get('name') for o in p.get('options', [])]}\n"
                  f"  description: {cat['chars']} chars, {cat['tables']} tables, parsed {len(cat['parsed'])} sizes, words={cat['words']}\n"
                  f"  page:        {page['chars']} chars, {page['tables']} tables, parsed {len(page['parsed'])} sizes, words={page['words']}, iframes={page['iframes']}\n"
                  f"  chart images: {cat['images'] or page['images'] or 'none'}\n"
                  f"  evidence: {cat['snippet'] or page['snippet'] or '(none)'}\n  -> {v}\n")
    return max(set(verdicts), key=verdicts.count) if verdicts else "no in-stock jeans found"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--store")
    a = ap.parse_args()
    sites = [s for s in app.load_sites() if not a.store or a.store.lower() in s["name"].lower()]
    with open("fit_diagnosis.txt", "w", encoding="utf-8") as out:
        for s in sites:
            res = run(s, out)
            print(f"{s['name'][:24]:24} {res}")
            sys.stdout.flush()
    print("\nSaved the evidence to fit_diagnosis.txt. Attach that file to the chat.")


if __name__ == "__main__":
    main()
