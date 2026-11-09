"""Price Scout: type a product, and it searches your list of stores for the best prices.
Run:  pip install -r requirements.txt && python app.py   ->  http://localhost:5000
"""
import json, os, re, secrets, sqlite3, threading, time, urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, g, jsonify, render_template_string, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# ---------------- Abuse protection ----------------
# Starting limits, per visitor IP. Override with env vars (no code change) once real traffic shows what is right.
LIMIT_DEFAULT = os.environ.get("LIMIT_DEFAULT", "300 per hour")        # every route
LIMIT_SEARCH = os.environ.get("LIMIT_SEARCH", "20 per minute")         # a search fans out to every store
LIMIT_ALERT_CREATE = os.environ.get("LIMIT_ALERT_CREATE", "10 per hour")
LIMIT_ALERT_CHECK = os.environ.get("LIMIT_ALERT_CHECK", "5 per hour")  # "Check now" button
MAX_ALERTS_PER_OWNER = int(os.environ.get("MAX_ALERTS_PER_OWNER", 20))
if os.environ.get("TRUST_PROXY"):    # number of proxies in front of the app (1 on most hosts); without it every visitor shares one IP
    _n = int(os.environ["TRUST_PROXY"])
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_n, x_proto=_n, x_host=_n)
limiter = Limiter(get_remote_address, app=app, default_limits=[LIMIT_DEFAULT],
                  storage_uri=os.environ.get("LIMITER_STORAGE", "memory://"), headers_enabled=True)   # use redis://... when running several workers


@app.errorhandler(429)
def too_many(e):
    return jsonify({"error": "Too many requests. Please wait a bit and try again."}), 429


# Alerts belong to the browser that made them: a random token in a cookie (no accounts yet).
OWNER_RE = re.compile(r"[A-Za-z0-9_-]{32}")


def owner_id():
    t = request.cookies.get("ps_owner", "")
    if OWNER_RE.fullmatch(t):
        return t
    if "new_owner" not in g:
        g.new_owner = secrets.token_urlsafe(24)       # 32 URL-safe characters
    return g.new_owner


@app.after_request
def set_owner_cookie(resp):
    t = g.get("new_owner")
    if t:
        resp.set_cookie("ps_owner", t, max_age=31536000, httponly=True, samesite="Lax", secure=request.is_secure)
    return resp
UA = "Mozilla/5.0 (compatible; PriceScout/1.0)"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
_robots = {}


def load_sites():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.json")) as f:   # works from any directory
        return json.load(f)


def parse_price(text):
    vals = [float(m.replace(",", "")) for m in PRICE_RE.findall(text or "")]
    return min(vals) if vals else None  # "Was $100 Now $80" -> 80


def allowed(url):
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        # Fetched with requests (certifi CA bundle): urllib's own rp.read() fails TLS on a python.org install
        # without "Install Certificates", and that failure used to mean "no robots.txt, allow everything".
        rp = urllib.robotparser.RobotFileParser(root + "/robots.txt")
        try:
            r = requests.get(root + "/robots.txt", headers=HEADERS, timeout=10)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None
        _robots[root] = rp
    rp = _robots[root]
    return True if rp is None else rp.can_fetch(UA, url)


def walk(o):
    if isinstance(o, list):
        for x in o:
            yield from walk(x)
    elif isinstance(o, dict):
        t = o.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            yield o
        for v in o.values():
            if isinstance(v, (dict, list)):
                yield from walk(v)


def from_jsonld(soup, base):
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for p in walk(data):
            offers = p.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") or offers.get("lowPrice")
            try:
                price = float(str(price).replace(",", ""))
            except Exception:
                continue
            if p.get("name") and p.get("url"):
                out.append((p["name"], price, urljoin(base, p["url"])))
    return out


def from_selectors(soup, site, base):
    out = []
    for el in soup.select(site["item"]):
        t, pr = el.select_one(site["title"]), el.select_one(site["price"])
        a = el.select_one(site.get("link", "a"))
        if t and pr and a and a.get("href"):
            text = pr.get_text(" ", strip=True)
            price = parse_price(text)
            if not price:  # yen / euro etc.: "30,000円(税込33,000円)" -> the first number, i.e. before Japanese tax
                m = re.search(r"\d{1,3}(?:[,.]\d{3})+|\d+", text)
                price = float(re.sub(r"[,.]", "", m.group(0))) if m else None
            if price:
                extra = {}
                img = el.select_one(site["image"]) if site.get("image") else None
                if img and (img.get("src") or img.get("data-src")):
                    extra["image"] = urljoin(base, img.get("src") or img.get("data-src"))
                if site.get("sizes"):  # optional; "size_soldout_class" marks sizes that are out of stock
                    soldout = site.get("size_soldout_class")
                    extra["sizes"] = [[s.get_text(strip=True), soldout not in (s.get("class") or [])]
                                      for s in el.select(site["sizes"]) if s.get_text(strip=True)]
                out.append((t.get_text(" ", strip=True), price, urljoin(base, a["href"]), extra))
    return out


def from_heuristic(soup, base):
    """No selectors configured: find links whose surrounding card contains a $ price."""
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True) or (a.img.get("alt", "") if a.img else "")
        if len(title) < 8 or a["href"] in seen:
            continue
        node = a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            price = parse_price(node.get_text(" ", strip=True))
            if price:
                seen.add(a["href"])
                out.append((title[:140], price, urljoin(base, a["href"])))
                break
    return out


def _plain(text):
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def matches(title, query):
    """Every word typed must appear in the title as a whole word, in any order, ignoring case and punctuation.
    No fuzzy or partial-word matching ('ron' does not find 'Iron Heart'). A typed word containing a digit may be
    the start of a model number ('0705' finds '0705SP'). Typing with no spaces ('momotaro0705') also works."""
    q = _plain(query)
    if not q:
        return True
    t = _plain(title)
    words = t.split()
    if all(any(w == x or (any(c.isdigit() for c in x) and w.startswith(x)) for w in words) for x in q.split()):
        return True
    sq = q.replace(" ", "")
    return len(sq) >= 5 and sq in t.replace(" ", "")


_catalog = {}  # base_url -> (fetched_at, products or None)


CATALOG_CAP = int(os.environ.get("CATALOG_CAP", 8000))   # max products read per store (250 per request)


def get_catalog(base):
    """Shopify stores publish their catalog at /products.json. Cached for an hour; None if the store has none."""
    hit = _catalog.get(base)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    products = None
    try:
        if allowed(f"{base}/products.json"):
            products, page = [], 1
            while len(products) < CATALOG_CAP:
                r = requests.get(f"{base}/products.json", params={"limit": 250, "page": page},
                                 headers=HEADERS, timeout=20)
                r.raise_for_status()
                batch = r.json()["products"]
                products += batch[:CATALOG_CAP - len(products)]
                if len(batch) < 250:
                    break
                page += 1
    except Exception:
        products = None
    products = products or None
    _catalog[base] = (time.time(), products)
    return products


def get_square_catalog(base, site):
    """Square Online stores (no /products.json) serve their storefront catalog as JSON. Needs `square_owner_id` and
    `square_site_id` in sites.json. Products are reshaped like Shopify's so from_shopify / measure_rows work unchanged."""
    key = base + "#square"
    hit = _catalog.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    url = f'{base}/app/store/api/v28/editor/users/{site["square_owner_id"]}/sites/{site["square_site_id"]}/products'
    products = None
    try:
        if allowed(url):
            products, page = [], 1
            while page <= 25:  # 200 a page, up to 5,000 products
                r = requests.get(url, params={"page": page, "per_page": 200, "include": "images,skus,categories"},
                                 headers=HEADERS, timeout=20)
                r.raise_for_status()
                j = r.json()
                for p in j["data"]:
                    if p.get("visibility") != "visible":
                        continue
                    skus = (p.get("skus") or {}).get("data") or []
                    variants = [{"option1": s.get("name") or "Default Title", "available": not s.get("sold_out"),
                                 "price": (s.get("price") or {}).get("current") or 0,
                                 "compare_at_price": (s.get("price") or {}).get("regular")} for s in skus]
                    if not variants:
                        pr = p.get("price") or {}
                        variants = [{"option1": "Default Title", "price": pr.get("low") or 0,
                                     "compare_at_price": pr.get("regular_low"),
                                     "available": not (p.get("badges") or {}).get("out_of_stock")}]
                    cats = [c.get("name", "") for c in (p.get("categories") or {}).get("data") or []]
                    imgs = [i.get("absolute_url") for i in (p.get("images") or {}).get("data") or [] if i.get("absolute_url")]
                    products.append({"title": p.get("name", ""), "vendor": "", "body_html": p.get("short_description"),
                                     "product_type": cats[0] if cats else "", "tags": cats,
                                     "url": p.get("absolute_site_link") or f'{base}/{p.get("site_link", "")}',
                                     "options": [{"name": "Size"}] if any(v["option1"] != "Default Title" for v in variants)
                                     else [{"name": "Title"}],
                                     "variants": variants, "images": [{"src": imgs[0]}] if imgs else []})
                pg = j.get("meta", {}).get("pagination", {})
                if page >= pg.get("total_pages", page):
                    break
                page += 1
    except Exception:
        products = None
    products = products or None
    _catalog[key] = (time.time(), products)
    return products


def variant_sizes(p):
    """[[size label, in_stock], ...] from the product's Size option."""
    names = [o.get("name", "") for o in p.get("options", [])]
    idx = next((i for i, n in enumerate(names) if "size" in n.lower() or "サイズ" in n), None)  # サイズ = size
    if idx is None and len(names) == 1 and names[0].lower() != "title":
        idx = 0
    if idx is None:
        return []
    seen = {}
    for v in p.get("variants", []):
        label = v.get(f"option{idx + 1}")
        if label and label != "Default Title":
            seen[label] = seen.get(label, False) or bool(v.get("available"))
    return [[k, a] for k, a in seen.items()]


FRAC = {"½": ".5", "¼": ".25", "¾": ".75", "⅛": ".125", "⅜": ".375", "⅝": ".625", "⅞": ".875"}


def parse_num(text):
    t = text
    for k, v in FRAC.items():
        t = t.replace(k, v)
    m = re.search(r"(\d+(?:\.\d+)?)(?:[\s-]+(\d+)/(\d+))?", t)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) and int(m.group(3)):
        val += int(m.group(2)) / int(m.group(3))
    return val


# Japanese size-chart labels (checked first; longer terms before shorter ones that they contain).
_JP_KEYS = (("sleeve", ("袖丈", "袖長", "裄丈", "裄", "スリーブ")),
            ("shoulder", ("肩幅", "肩巾", "ショルダー")),
            ("chest", ("身幅", "胸囲", "胸周り", "胸回り", "胸幅", "バスト")),
            ("length", ("着丈", "総丈", "背丈", "後ろ丈", "後丈", "ネック下", "全長")),
            ("back_rise", ("後股上", "後ろ股上", "バックライズ")),
            ("front_rise", ("前股上", "フロントライズ", "股上", "ライズ")),
            ("waist", ("ウエスト", "ウェスト", "胴回り", "胴周り")),
            ("thigh", ("ワタリ", "わたり", "渡り", "太もも", "もも")),
            ("knee", ("ひざ", "ヒザ", "膝")),
            ("leg_opening", ("裾幅", "裾周り", "裾回り", "裾口", "すそ", "裾")))


def measure_key(text):
    for key, words in _JP_KEYS:
        if any(w in text for w in words):
            return key
    t = re.sub(r"[^a-z ]", " ", text.lower())
    if "rise" in t:
        return "back_rise" if "back" in t else "front_rise"
    if "sleeve" in t:                      # "sleeve length" is the sleeve, not the body length
        return "sleeve"
    if "shoulder" in t:
        return "shoulder"
    for key, words in (("chest", ("chest", "bust", "pit to pit", "pit pit", "armpit", "underarm", "ptp")),
                       ("waist", ("waist",)), ("thigh", ("thigh",)), ("knee", ("knee",)),
                       ("leg_opening", ("opening", "hem", "bottom")), ("length", ("length", "back len", "body len"))):
        if any(w in t for w in words):
            return key
    return None


SIZE_LINE = re.compile(r"^\s*(?:size\s*)?(w?\d{2}(?:\s*[x/]\s*\d{2})?|(?:[2-5]xl|x{0,3}[sl]|m|small|medium|large)(?![a-z]))\s*[:\-\u2013|]\s*(.+)$", re.I)
PAIR = re.compile(r"(waist|thigh|front\s*rise|back\s*rise|rise|knee|leg\s*opening|opening|hem|chest|bust|pit\s*to\s*pit|shoulders?|sleeve(?:\s*length)?|(?:back|body|total)\s*length|length)\s*[:=]?\s*"
                  r"(\d+(?:\.\d+)?(?:\s+\d+/\d+|[½¼¾⅛⅜⅝⅞])?|[½¼¾⅛⅜⅝⅞])", re.I)


def _slash_num(seg):
    """'23,5cm' -> (23.5, 'cm'); '30inc' -> (30.0, 'in'); '12' -> (12.0, '')."""
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|inc(?:hes)?|in|\")?", seg, re.I)
    if not m:
        return None, ""
    unit = (m.group(2) or "").lower()
    return float(m.group(1).replace(",", ".")), ("cm" if unit == "cm" else "in" if unit else "")


def _slash_charts(lines, out):
    """BEARS TOKYO style: a header 'Size/waist/front rise/...' then one line per size '30inc/76cm/30cm/...'."""
    for i, line in enumerate(lines):
        head = [h.strip() for h in line.split("/")]
        if len(head) < 4 or not head[0].lower().startswith("size"):
            continue
        keys = [measure_key(h) for h in head[1:]]
        if sum(k is not None for k in keys) < 2:
            continue
        for row in lines[i + 1:i + 40]:
            seg = [x.strip() for x in row.split("/")]
            if len(seg) != len(head):
                break
            size, _ = _slash_num(seg[0])
            if size is None:
                break
            cm_row = any("cm" in x.lower() for x in seg[1:])
            label = str(int(size)) if size == int(size) else str(size)
            for k, cell in zip(keys, seg[1:]):
                v, unit = _slash_num(cell)
                if k and v:
                    inches = v / 2.54 if (unit == "cm" or (not unit and cm_row)) else v
                    out.setdefault(label, {}).setdefault(k, round(inches, 2))


BASE_SIZE = re.compile(r"measurements?\s+(?:are\s+)?based\s+on\s+(?:a\s+)?(?:size\s+)?(w?\d{2}|[2-5]xl|x{0,3}[sl]|m|small|medium|large)(?![a-z0-9])", re.I)


def parse_charts(html):
    """{size label: {measurement: inches}} read from size-chart tables in a product description."""
    if not html:
        return {}
    out = {}
    soup = BeautifulSoup(html, "html.parser")
    for line in soup.get_text("\n").splitlines():  # plain-text charts: "32: Waist 16.5, Thigh 12.25, ..."
        m = SIZE_LINE.match(line)
        if not m:
            continue
        div = 2.54 if re.search(r"\bcm\b", line, re.I) else 1.0
        vals = {}
        for name, num in PAIR.findall(m.group(2)):
            k, v = measure_key(name), parse_num(num)
            if k and v:
                vals.setdefault(k, round(v / div, 2))
        if len(vals) >= 2:
            for k, v in vals.items():
                out.setdefault(m.group(1).strip(), {}).setdefault(k, v)
    lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
    _slash_charts(lines, out)
    for line in lines:      # one garment size only: 'Front rise: 11 1/4", Knee: 18", Leg opening: 16", Measurements based on size 32'
        m = BASE_SIZE.search(line)
        if not m:
            continue
        div = 2.54 if re.search(r"\bcm\b", line, re.I) else 1.0
        vals = {}
        for name, num in PAIR.findall(line):
            k, v = measure_key(name), parse_num(num)
            if k and v:
                vals.setdefault(k, round(v / div, 2))
        if len(vals) >= 2:
            for k, v in vals.items():
                out.setdefault(m.group(1).strip(), {}).setdefault(k, v)
    for tb in soup.find_all("table"):
        text = tb.get_text(" ").lower()
        metric = re.search(r"\bcm\b|センチ", text) or re.search(r"[ぁ-んァ-ヶ一-龥]", text)   # Japanese charts are in cm even when the unit is not printed
        div = 2.54 if metric and not re.search(r"\binch|\bin\b|\"", text) else 1.0
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in tb.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        head = [measure_key(c) for c in rows[0]]
        side = [measure_key(r[0]) for r in rows[1:]]
        if sum(k is not None for k in head[1:]) >= 2:        # one row per size
            for r in rows[1:]:
                for k, cell in zip(head[1:], r[1:]):
                    v = parse_num(cell)
                    if k and v:
                        out.setdefault(r[0].strip(), {}).setdefault(k, round(v / div, 2))
        elif sum(k is not None for k in side) >= 2:          # one row per measurement
            for r, k in zip(rows[1:], side):
                for size, cell in zip(rows[0][1:], r[1:]):
                    v = parse_num(cell)
                    if k and v and size.strip():
                        out.setdefault(size.strip(), {}).setdefault(k, round(v / div, 2))
    for vals in out.values():        # the app compares flat (across-the-garment) waist, ~16-18 in; a chart giving the full circumference is halved
        if vals.get("waist") and vals["waist"] > 26:
            vals["waist"] = round(vals["waist"] / 2, 2)
        if vals.get("chest") and vals["chest"] > 30:     # flat chest (pit to pit) is ~18-28 in; a full chest circumference (~36-56 in) is halved
            vals["chest"] = round(vals["chest"] / 2, 2)
    return out


_SIZE_WORDS = {"small": "s", "medium": "m", "large": "l", "xsmall": "xs", "xlarge": "xl", "xxlarge": "xxl", "2xl": "xxl", "3xl": "xxxl"}


def measure_rows(p, extra=None):
    """Chart rows that match a real variant: [{size (the variant's own label), in_stock, waist, thigh, chest, ...}]."""
    def clean(l):
        t = re.sub(r"^w(?=\d)", "", re.sub(r"[\s\-]+", "", l.lower()))
        return _SIZE_WORDS.get(t, t)
    bare = lambda l: clean(re.sub(r"\(.*?\)", "", l))        # "36(S)" -> "36"
    avail = {}
    for l, a in variant_sizes(p):
        avail.setdefault(clean(l), (l, a))
        avail.setdefault(bare(l), (l, a))
    out = []
    for label, vals in {**(extra or {}), **parse_charts(p.get("body_html"))}.items():
        hit = avail.get(clean(label)) or avail.get(bare(label))
        if hit:
            out.append({"size": hit[0], "in_stock": hit[1], **vals})
    return out


# ---- Garment categories ----
CATEGORIES = ["Bottoms", "Tops", "Outerwear", "Footwear", "Bags", "Accessories", "Other"]
_TYPE_ALIASES = {"top": "Tops", "tops": "Tops", "bottom": "Bottoms", "bottoms": "Bottoms", "outerwear": "Outerwear",
                 "footwear": "Footwear", "shoes": "Footwear", "bag": "Bags", "bags": "Bags",
                 "accessory": "Accessories", "accessories": "Accessories"}
# First match wins, so specific garments come before general ones ("denim jacket" is Outerwear, not Bottoms).
# To change how items are sorted, edit these word lists.
_RULES = [
    ("Other", r"gift\s*cards?|hemming|services?|alterations?"),
    ("Footwear", r"boots?|shoes?|sneakers?|loafers?|sandals?|derbys?|oxfords?|moccasins?|mocs?|clogs?|slippers?|trainers?|footwear|espadrilles?|brogues?|chukkas?"),
    ("Bags", r"bags?|backpacks?|totes?|duffles?|duffels?|briefcases?|satchels?|rucksacks?|messengers?|pouch(?:es)?|waist\s*packs?|holdalls?"),
    ("Accessories", r"socks?|hats?|caps?(?!\s*(?:toe|sleeve))|beanies?|belts?|wallets?|scarf|scarves|bandanas?|gloves?|mittens?|keychains?|key\s*rings?|lanyards?|sunglasses|suspenders|bracelets?|necklaces?|earrings?|insoles?"),
    ("Outerwear", r"jackets?|coats?|blazers?|blousons?|parkas?|haoris?|vests?|coveralls?|anoraks?|bombers?|overcoats?|shackets?|windbreakers?|ponchos?|outerwear|waistcoats?"),
    ("Tops", r"shirts?|t-?shirts?|tees?|polos?|henleys?|sweaters?|sweatshirts?|hoodies?|crew\s*necks?|cardigans?|knitwear|jumpers?|pullovers?|turtlenecks?|tanks?|blouses?|button-?ups?|button-?downs?|overshirts?|long\s*sleeves?|short\s*sleeves?"),
    ("Bottoms", r"jeans?|pants?|trousers?|shorts|chinos?|slacks?|overalls?|dungarees?|joggers?|cargos?|culottes?|skirts?|leggings?|bottoms?|sweatpants|fatigues?"),
]
_COMPILED = [(c, re.compile(r"\b(?:" + p + r")\b")) for c, p in _RULES]
_STRIP = re.compile(r"boot[\s-]*cut|shoe[\s-]*cut|shoe\s*trees?|shoe\s*horns?|boot\s*trees?|boot(?=\s*socks?)")  # not footwear
_DENIM = re.compile(r"denim|selvedge|selvage|boot[\s-]*cut|shoe[\s-]*cut|flare|(?:straight|slim|wide|loose|relaxed)\s+fit|tapered")


def categorize(product_type="", title="", tags=()):
    """One of CATEGORIES. Looks at the store's own product type first, then the title, then its tags."""
    t = (product_type or "").strip().lower()
    if t in _TYPE_ALIASES:
        return _TYPE_ALIASES[t]
    for text in (product_type, title, " ".join(tags or [])):
        s = _STRIP.sub(" ", (text or "").lower())
        for cat, rx in _COMPILED:
            if rx.search(s):
                return cat
    for text in (title, product_type):  # "…Selvedge Denim – Regular Straight Fit" never says "jeans"
        if _DENIM.search((text or "").lower()):
            return "Bottoms"
    return "Other"


def from_shopify(base, products):
    out = []
    for p in products:
        variants = p.get("variants", [])
        live = [v for v in variants if v.get("available")]
        pool = live or variants
        if not pool:
            continue
        v = min(pool, key=lambda x: float(x["price"]))
        price, was = float(v["price"]), float(v.get("compare_at_price") or 0)
        vendor = p.get("vendor") or ""
        if re.sub(r"\W", "", vendor.lower()) and re.sub(r"\W", "", vendor.lower()) in base.lower():
            vendor = ""  # the store's own name as vendor (Reggieshop) says nothing; the brand is in the title
        title = (f"{vendor}: " if vendor else "") + p["title"] + ("" if live else " (sold out)")
        tags = p.get("tags") or []
        tags = tags.split(",") if isinstance(tags, str) else tags
        img = (p.get("images") or [{}])[0].get("src")
        out.append((title, price, p.get("url") or f'{base}/products/{p["handle"]}', {
            "match": f'{title} {p.get("product_type", "")} {" ".join(tags)}',
            "sizes": variant_sizes(p),
            "category": categorize(p.get("product_type", ""), p["title"], tags),
            "measures": lambda extra=None, p=p: measure_rows(p, extra),   # parsed lazily, only for matching products
            "was": was if was > price else None,
            "codes": sorted({str(x.get("barcode")).strip() for x in variants if len(str(x.get("barcode") or "").strip()) >= 8}),
            "image": img and img + ("&" if "?" in img else "?") + "width=160",
        }))
    return out


# ---- Landed-cost estimates (all rough; shipping to the US). Override per store in sites.json. ----
DEST = "US"
DEFAULT_DUTY_PCT = 16.6                       # approx. US duty on cotton jeans; other goods differ
DEFAULT_SHIP_USD = {"domestic": 10.0, "intl": 35.0}


def landed(site, usd, currency, country=None):
    """(shipping, duty) in USD. Per-store keys: origin, shipping_usd, free_shipping_over_usd, duty_pct, duties_included.
    `country` is the detected ships-from country; sites.json 'origin' overrides it."""
    origin = (norm_country(site.get("origin")) or country or ("US" if currency == "USD" else "INTL")).upper()
    domestic = origin == DEST
    ship = site.get("shipping_usd", DEFAULT_SHIP_USD["domestic" if domestic else "intl"])
    free_over = site.get("free_shipping_over_usd")
    if free_over is not None and usd >= free_over:
        ship = 0.0
    duty_pct = 0.0 if domestic or site.get("duties_included") else site.get("duty_pct", DEFAULT_DUTY_PCT)
    return round(ship, 2), round(usd * duty_pct / 100, 2)


_currency = {}   # base_url -> ISO currency code
_fx = [0, {}]    # [fetched_at, {code: units per 1 USD}]


COUNTRY_NAMES = {
    "US": "United States", "CA": "Canada", "MX": "Mexico", "GB": "United Kingdom", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "IT": "Italy", "ES": "Spain", "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "SE": "Sweden",
    "DK": "Denmark", "PL": "Poland", "PT": "Portugal", "CH": "Switzerland", "EU": "European Union", "JP": "Japan",
    "KR": "South Korea", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan", "SG": "Singapore", "AU": "Australia",
    "NZ": "New Zealand", "TH": "Thailand", "ID": "Indonesia", "TR": "Turkey",
}
_NAME_TO_CODE = {v.lower(): k for k, v in COUNTRY_NAMES.items()}
_NAME_TO_CODE.update({"usa": "US", "united states of america": "US", "uk": "GB", "great britain": "GB", "england": "GB",
                      "deutschland": "DE", "nippon": "JP", "korea": "KR"})


def norm_country(x):
    """ISO-2 code from a code or an English name; None when unknown/blank."""
    x = (x or "").strip()
    if not x:
        return None
    return x.upper() if x.upper() in COUNTRY_NAMES else _NAME_TO_CODE.get(x.lower(), x.upper() if len(x) == 2 else None)


def country_name(code):
    return COUNTRY_NAMES.get(code, code) if code else None


_meta = {}   # base_url -> Shopify /meta.json dict ({} if unavailable)


def shop_meta(base):
    if base not in _meta:
        try:
            r = requests.get(base + "/meta.json", headers=HEADERS, timeout=10)
            r.raise_for_status()
            j = r.json()
            _meta[base] = j if isinstance(j, dict) else {}
        except Exception:
            _meta[base] = {}
    return _meta[base]


def store_currency(site, base, shopify):
    """(currency, assumed). sites.json 'currency' wins; Shopify stores report theirs at /meta.json."""
    if site.get("currency"):
        return site["currency"].upper(), False
    if not shopify:
        return "USD", True
    cur = shop_meta(base).get("currency")
    return (cur.upper(), False) if cur else ("USD", True)


def store_country(site, base, shopify):
    """ISO-2 country the store ships from. sites.json 'origin' wins; else the country a Shopify store reports in
    /meta.json (the shop's registered country, a good but not perfect proxy for where orders ship from)."""
    return norm_country(site.get("origin")) or (norm_country(shop_meta(base).get("country")) if shopify else None)


def usd_rates():
    """Units of each currency per 1 USD, cached 12 hours. Free, keyless sources; second is a fallback."""
    if _fx[1] and time.time() - _fx[0] < 43200:
        return _fx[1]
    for url in ("https://open.er-api.com/v6/latest/USD", "https://api.frankfurter.dev/v1/latest?base=USD"):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            rates = r.json()["rates"]
            rates["USD"] = 1.0
            _fx[0], _fx[1] = time.time(), rates
            return rates
        except Exception:
            continue
    return _fx[1] or {"USD": 1.0}


_pages = {}  # product url -> (fetched_at, charts parsed from the page)


def page_charts(url):
    """Size charts that live on the product page itself (not in the catalog description). Cached 6 hours."""
    hit = _pages.get(url)
    if hit and time.time() - hit[0] < 21600:
        return hit[1]
    charts = {}
    try:
        if allowed(url):
            r = requests.get(url, headers=HEADERS, timeout=12)
            r.raise_for_status()
            charts = parse_charts(r.text)
    except Exception:
        pass
    _pages[url] = (time.time(), charts)
    return charts


MAX_PAGE_FETCHES = 10  # per store, per search; results are cached


def search_site(site, query, category=None):
    name, base = site["name"], site["base_url"].rstrip("/")
    try:
        products = (None if site.get("type") == "html" else
                    get_square_catalog(base, site) if site.get("type") == "square" else get_catalog(base))
        if products:
            found, url = from_shopify(base, products), base
        elif not query:  # browsing a category needs a store catalog; search pages can't list everything
            return name, [], "no matching results"
        else:  # not a Shopify store (or catalog hidden): scrape its search page
            url = (site.get("search_url") or base + "/search?q={q}").format(
                q=quote_plus(query, encoding=site.get("search_encoding", "utf-8"), errors="ignore"))
            if not allowed(url):
                return name, [], "no public catalog, and its search page is blocked by robots.txt"
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            found = (from_selectors(soup, site, url) if "item" in site else []) \
                or from_jsonld(soup, url) or from_heuristic(soup, url)
        cur, assumed = store_currency(site, base, bool(products))
        ships_from = store_country(site, base, bool(products))
        offers = []
        for t, p, u, *rest in found:
            x = rest[0] if rest else {}
            if not matches(x.get("match") or t, query):
                continue
            cat = x.get("category") or categorize("", t)
            if category and cat != category:
                continue
            offers.append({"site": name, "title": t, "price": p, "url": u, "currency": cur,
                           "assumed": assumed, "sizes": x.get("sizes", []), "category": cat,
                           "was": x.get("was"), "image": x.get("image"), "measures": [],
                           "ships_from": ships_from, "codes": x.get("codes", []), "_fill": x.get("measures")})
        return name, offers, None if offers else "no matching results"
    except Exception as e:
        return name, [], str(e)[:120]


def fill_measures(offers):
    """Read size charts for the listings we are about to return, then drop the private helper."""
    pending = []
    for o in offers:
        fn = o.pop("_fill", None)
        if fn:
            o["measures"] = fn()
            if not o["measures"] and o["sizes"]:
                pending.append((o, fn))
    per, jobs = {}, []
    for o, fn in pending:  # charts that live on the product page: a few per store, cached for hours
        if per.get(o["site"], 0) < MAX_PAGE_FETCHES:
            per[o["site"]] = per.get(o["site"], 0) + 1
            jobs.append((o, fn))

    def grab(job):
        o, fn = job
        charts = page_charts(o["url"])
        if charts:
            o["measures"] = fn(charts)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(grab, jobs))


BROWSE_CAP, SEARCH_CAP = 200, 600   # most listings returned per request


_G_STOP = {"jeans", "jean", "pants", "the", "a", "and", "in", "of", "new", "restock", "sold", "out"}


def group_key(title):
    """Name-based product key: 'Vendor: Model' with punctuation, store filler and word order ignored.
    Colourway / wash words are kept on purpose, so a different wash stays a separate product."""
    t = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return " ".join(sorted(w for w in t.split() if w not in _G_STOP))


def assign_groups(offers):
    """Give each offer a 'gid'. Offers share one when their names match OR they share a barcode (UPC/EAN),
    which is the strongest signal. Union-find over both."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for o in offers:
        keys = ["n:" + group_key(o["title"])] + ["b:" + c for c in o.pop("codes", [])]
        for k in keys[1:]:
            parent[find(keys[0])] = find(k)
        find(keys[0])
        o["_k"] = keys[0]
    ids = {}
    for o in offers:
        o["gid"] = ids.setdefault(find(o.pop("_k")), f"g{len(ids)}")


def run_search(q, category=None):
    """(offers sorted cheapest first, notes, info). With no search text, a category is browsed instead."""
    sites = load_sites()
    with ThreadPoolExecutor(12) as ex:
        res = list(ex.map(lambda site: search_site(site, q, category), sites))
    offers = [o for _, found, _ in res for o in found]
    rates, by_name = usd_rates(), {st["name"]: st for st in sites}
    for o in offers:
        rate = rates.get(o["currency"])
        o["usd"] = round(o["price"] / rate, 2) if rate else None
        o["was_usd"] = round(o["was"] / rate, 2) if rate and o["was"] else None
        o["sold_out"] = o["title"].endswith("(sold out)")
        o["ships_from_name"] = country_name(o.get("ships_from"))
        if o["usd"] is None:
            o["ship"] = o["duty"] = o["total"] = None
        else:
            o["ship"], o["duty"] = landed(by_name[o["site"]], o["usd"], o["currency"], o.get("ships_from"))
            o["total"] = round(o["usd"] + o["ship"] + o["duty"], 2)
    # cheapest first: in-stock items by estimated total, then anything sold out
    offers.sort(key=lambda o: (o["sold_out"], o["total"] if o["total"] is not None else 1e18))
    browse = not q
    cap = BROWSE_CAP if browse else SEARCH_CAP
    info = {"browse": browse, "category": category, "total_matches": len(offers), "truncated": len(offers) > cap}
    offers = offers[:cap]
    assign_groups(offers)
    fill_measures(offers)
    notes = [f"{n}: {e}" for n, _, e in res if e and not (browse and e == "no matching results")]
    return offers, notes, info


@app.get("/api/search")
@limiter.limit(LIMIT_SEARCH)
def api_search():
    q = request.args.get("q", "").strip()[:120]
    cat = request.args.get("category", "").strip()
    cat = cat if cat in CATEGORIES else None
    if not q and not cat:
        return jsonify({"offers": [], "notes": [], "browse": False, "category": None, "total_matches": 0, "truncated": False})
    offers, notes, info = run_search(q, cat)
    return jsonify({"offers": offers, "notes": notes, **info})


# ---------------- Price alerts (SQLite; checked hourly while the app runs) ----------------
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.db")


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, query TEXT, size TEXT,
        max_total REAL, email TEXT, created REAL, checked REAL, best_total REAL, best_title TEXT,
        best_site TEXT, best_url TEXT, notified_total REAL, notified_at REAL)""")
    try:
        c.execute("ALTER TABLE alerts ADD COLUMN fit TEXT")  # older databases
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE alerts ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE alerts ADD COLUMN owner TEXT")   # who may see/delete it; older alerts have none and are not shown
    except sqlite3.OperationalError:
        pass
    return c


def size_match(label, want):
    norm = lambda x: re.sub(r"^w(?=\d)", "", re.sub(r"\s+", "", x.lower()))
    return norm(want) in [norm(p) for p in [label] + re.split(r"\s*[/,]\s*|\s+x\s+", label.lower())]


def want_sizes(want):
    """'32' or '32,34' (several sizes, any of them) -> ['32', '34']."""
    return [x.strip() for x in re.split(r"[,;]+", want or "") if x.strip()]


def has_size(o, want):
    ws = want_sizes(want)
    return not ws or any(ok and any(size_match(label, w) for w in ws) for label, ok in o["sizes"])


BOTTOM_KEYS = ("waist", "thigh", "front_rise", "back_rise", "knee", "leg_opening")
TOP_KEYS = ("chest", "shoulder", "length", "sleeve")
FIT_KEYS = BOTTOM_KEYS + TOP_KEYS
KEYS_BY_CATEGORY = {"Bottoms": BOTTOM_KEYS, "Tops": TOP_KEYS, "Outerwear": TOP_KEYS}   # Outerwear shares the upper-body chart


def fit_ranges(fit):
    out = []
    for k in FIT_KEYS:
        lo, hi = (fit.get(k) or [None, None])[:2]
        if lo is not None or hi is not None:
            out.append((k, lo, hi))
    return out


def fit_ok(o, want, ranges):
    """Fit ranges apply per category: waist/thigh/rise/knee/leg opening to Bottoms, chest/shoulder/length/sleeve to
    Tops and Outerwear. With any range set, a product needs a range for its own category and a size row inside it."""
    if ranges:
        keys = KEYS_BY_CATEGORY.get(o.get("category"), ())
        ranges = [r for r in ranges if r[0] in keys]
        if not ranges:
            return False
    if not ranges:
        return has_size(o, want)
    ws = want_sizes(want)
    return any(m["in_stock"] and (not ws or any(size_match(m["size"], w) for w in ws)) and
               all(m.get(k) is not None and (lo is None or m[k] >= lo) and (hi is None or m[k] <= hi)
                   for k, lo, hi in ranges) for m in o["measures"])


def check_alert(r):
    offers, _, _ = run_search(r["query"])
    ok = [o for o in offers if not o["sold_out"] and o["total"] is not None
          and (not r.get("category") or o["category"] == r["category"]) and fit_ok(o, r["size"], fit_ranges(json.loads(r.get("fit") or "{}")))]
    best = min(ok, key=lambda o: o["total"], default=None)
    hit = best is not None and (r["max_total"] is None or best["total"] <= r["max_total"])
    notified, notified_at = r["notified_total"], r["notified_at"]
    if hit and (notified is None or best["total"] < notified - 0.01):  # new hit, or a further drop
        notified, notified_at = best["total"], time.time()
    elif not hit:
        notified = None  # re-arm so a later hit notifies again
    with db() as c:
        c.execute("""UPDATE alerts SET checked=?, best_total=?, best_title=?, best_site=?, best_url=?,
                     notified_total=?, notified_at=? WHERE id=?""",
                  (time.time(), best and best["total"], best and best["title"], best and best["site"],
                   best and best["url"], notified, notified_at, r["id"]))


def check_all(owner=None):
    """Check every alert (the hourly job), or only one visitor's alerts."""
    with db() as c:
        rows = [dict(x) for x in c.execute("SELECT * FROM alerts" + (" WHERE owner=?" if owner else ""), (owner,) if owner else ())]
    for r in rows:
        try:
            check_alert(r)
        except Exception as e:
            print("alert check failed:", e)


def alert_loop():
    every = float(os.environ.get("ALERT_INTERVAL_MIN", 60)) * 60
    while True:
        time.sleep(every)
        check_all()


@app.get("/api/alerts")
def alerts_list():
    with db() as c:
        rows = [dict(x) for x in c.execute(
            """SELECT id, query, size, max_total, created, checked, best_total, best_title, best_site, best_url,
                      notified_total, notified_at, fit, category FROM alerts WHERE owner=? ORDER BY id DESC""", (owner_id(),))]
    return jsonify({"alerts": rows})


@app.post("/api/alerts")
@limiter.limit(LIMIT_ALERT_CREATE)
def alerts_add():
    d = request.get_json() or {}
    q = (d.get("query") or "").strip()[:120]
    try:
        mx = float(d["max_total"]) if d.get("max_total") not in (None, "") else None
    except ValueError:
        return jsonify({"error": "bad price"}), 400
    if not q:
        return jsonify({"error": "no search"}), 400
    cat = d.get("category") if d.get("category") in CATEGORIES else ""
    fit = {}
    for k in FIT_KEYS:
        try:
            pair = [None if v in (None, "") else float(v) for v in (d.get("fit") or {}).get(k, [None, None])[:2]]
        except (TypeError, ValueError):
            continue
        if any(v is not None for v in pair):
            fit[k] = pair
    me = owner_id()
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM alerts WHERE owner=?", (me,)).fetchone()[0] >= MAX_ALERTS_PER_OWNER:
            return jsonify({"error": f"You can have up to {MAX_ALERTS_PER_OWNER} alerts. Delete one first."}), 400
        cur = c.execute("INSERT INTO alerts (query, size, max_total, created, fit, category, owner) VALUES (?,?,?,?,?,?,?)",
                        (q, (d.get("size") or "").strip()[:60], mx,
                         time.time(), json.dumps(fit), cat, me))
        row = dict(c.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)).fetchone())
    threading.Thread(target=check_alert, args=(row,), daemon=True).start()
    return jsonify({"ok": True})


@app.delete("/api/alerts/<int:aid>")
def alerts_del(aid):
    with db() as c:
        c.execute("DELETE FROM alerts WHERE id=? AND owner=?", (aid, owner_id()))
    return jsonify({"ok": True})


@app.post("/api/alerts/check")
@limiter.limit(LIMIT_ALERT_CHECK)
def alerts_check_now():
    check_all(owner_id())          # only the caller's own alerts
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template_string(PAGE, sites=[s["name"] for s in load_sites()])


PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Price Scout</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Host+Grotesk:wght@300..800&display=swap" rel="stylesheet">
<style>
:root{--dp-color-ground:#f5f4ee;--dp-color-ink:#000000;--dp-color-accent:#ff0000;--dp-color-field:#0000ff;--dp-color-field-support:#ffff00;--dp-color-quiet:#e8e8e8;--dp-color-on-field:#ffffff;
--dp-typography-face:"Bauhaus Host","Host Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;--dp-typography-body:18px;--dp-typography-reading:17px;--dp-typography-label:15px;--dp-typography-caption:14px;
--dp-typography-weight-display:800;--dp-typography-weight-action:750;--dp-typography-weight-label:650;--dp-typography-weight-reading:450;--dp-typography-reading-leading:1.5;
--dp-spacing-gutter:clamp(22px,4.45cqi,72px);--dp-radius-square:0;--dp-radius-circle:50%;--dp-border-rule:4px;--dp-border-rule-compact:3px;--dp-motion-interaction:0s;color-scheme:light}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;container-type:inline-size;background:var(--dp-color-ground);color:var(--dp-color-ink);font-family:var(--dp-typography-face);font-size:var(--dp-typography-body);font-weight:var(--dp-typography-weight-reading);line-height:var(--dp-typography-reading-leading);padding-bottom:env(safe-area-inset-bottom,0px)}
h1{margin:0 0 20px;padding-right:clamp(56px,10cqi,128px);font-size:clamp(44px,9cqi,104px);font-weight:var(--dp-typography-weight-display);line-height:.92;letter-spacing:-.045em}
a{color:inherit;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}a:hover{background:var(--dp-color-field-support)}
.note{background:var(--dp-color-field-support);border-bottom:var(--dp-border-rule) solid var(--dp-color-ink);padding:12px var(--dp-spacing-gutter);font-size:var(--dp-typography-label);font-weight:var(--dp-typography-weight-label)}
.hero{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(0,1fr);border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}
.plane-text{position:relative;min-width:0;padding:clamp(28px,5cqi,64px) var(--dp-spacing-gutter) 28px;border-right:var(--dp-border-rule) solid var(--dp-color-ink)}
.tri{position:absolute;top:0;right:0;width:clamp(56px,10cqi,128px);aspect-ratio:1;background:var(--dp-color-field);clip-path:polygon(0 0,100% 0,100% 100%);pointer-events:none}
.plane-red{background:var(--dp-color-accent);display:grid;place-items:center;padding:32px var(--dp-spacing-gutter)}
.bar{margin:0}
.mut{color:#3d3d3d;font-size:var(--dp-typography-caption);font-weight:var(--dp-typography-weight-reading)}
.plane-text .mut{margin:14px 0 0}
input,select{font:inherit;font-size:16px;font-weight:var(--dp-typography-weight-label);padding:10px 12px;border:var(--dp-border-rule-compact) solid var(--dp-color-ink);border-radius:var(--dp-radius-square);background:#fff;color:var(--dp-color-ink);max-width:100%}
#q{width:100%;padding:16px;font-size:var(--dp-typography-body);border-width:var(--dp-border-rule)}
input[type=checkbox]{width:22px;height:22px;padding:0;accent-color:#000;vertical-align:-5px;margin:0 6px 0 0}
.sm{width:116px}.md{width:200px}
label{font-size:var(--dp-typography-label);font-weight:var(--dp-typography-weight-label)}
button{font:inherit;font-size:var(--dp-typography-body);font-weight:var(--dp-typography-weight-action);cursor:pointer;border:var(--dp-border-rule) solid var(--dp-color-ink);border-radius:var(--dp-radius-square);background:#fff;color:var(--dp-color-ink);padding:12px 22px;box-shadow:6px 6px 0 var(--dp-color-ink)}
button:hover:not(:disabled){background:var(--dp-color-field-support)}
button:active:not(:disabled){transform:translate(6px,6px);box-shadow:0 0 0 var(--dp-color-ink)}
button:disabled{cursor:wait;background:var(--dp-color-quiet);color:#3d3d3d}
button.blue{background:var(--dp-color-field);color:var(--dp-color-on-field)}
button.blue:hover:not(:disabled){background:var(--dp-color-ink);color:var(--dp-color-on-field)}
button.ghost{padding:6px 12px;font-size:var(--dp-typography-label);border-width:var(--dp-border-rule-compact);box-shadow:4px 4px 0 var(--dp-color-ink)}
button.ghost:active:not(:disabled){transform:translate(4px,4px)}
button.circle{width:clamp(150px,16cqi,220px);aspect-ratio:1;padding:0;border-radius:var(--dp-radius-circle);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;font-size:22px;box-shadow:8px 8px 0 var(--dp-color-ink)}
button.circle:active:not(:disabled){transform:translate(8px,8px)}
button.circle .arrow{font-size:40px;line-height:1}
:focus-visible{outline:4px solid var(--dp-color-ink);outline-offset:3px}
input:focus-visible,select:focus-visible{outline-color:var(--dp-color-field);outline-offset:2px}
.ctl{display:flex;flex-wrap:wrap;align-items:center;gap:12px 24px;padding:18px var(--dp-spacing-gutter);background:var(--dp-color-field-support);border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}
#abar{background:var(--dp-color-quiet);border-top:var(--dp-border-rule) solid var(--dp-color-ink)}
details{background:#fff;border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}
summary{list-style:none;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:16px;padding:16px var(--dp-spacing-gutter);font-size:var(--dp-typography-body);font-weight:var(--dp-typography-weight-action)}
summary::-webkit-details-marker{display:none}
summary::after{content:"+";font-size:32px;font-weight:800;line-height:1}
details[open]>summary::after{content:"\2212"}
details[open]>summary{border-bottom:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
.fitgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px 20px;padding:18px var(--dp-spacing-gutter) 6px}
.fitgrid input{width:76px}
details>p.mut{margin:0;padding:6px var(--dp-spacing-gutter) 18px}
.fitlab{margin:0;padding:12px var(--dp-spacing-gutter) 0;font-weight:var(--dp-typography-weight-action);color:var(--dp-color-ink)}
.fitlab+.fitgrid{padding-top:8px}
.card{background:#fff;border:0;border-bottom:var(--dp-border-rule) solid var(--dp-color-ink);padding:20px var(--dp-spacing-gutter) 24px;margin:0;overflow-x:auto}
details .card{border-bottom:0;padding-top:12px}
.scroll{overflow-x:auto}
table{width:100%;min-width:900px;border-collapse:collapse;font-size:16px}
th{background:var(--dp-color-ink);color:var(--dp-color-on-field);font-size:var(--dp-typography-label);font-weight:var(--dp-typography-weight-label);letter-spacing:.04em;text-transform:uppercase;text-align:left;padding:12px}
td{padding:12px;text-align:left;vertical-align:top;border-bottom:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
.best td{background:var(--dp-color-field-support);font-weight:var(--dp-typography-weight-action)}
.best td:first-child{box-shadow:inset 10px 0 0 var(--dp-color-ink)}
tr.more td{background:var(--dp-color-ground);padding:8px 12px}
tr.sub td{background:var(--dp-color-ground)}tr.sub td:first-child{box-shadow:inset 10px 0 0 var(--dp-color-field)}tr.sub .thumb{opacity:.85}
.tot{font-size:22px;font-weight:var(--dp-typography-weight-display);letter-spacing:-.02em}
.thumb{width:72px;height:72px;flex:none;display:flex;align-items:center;justify-content:center;object-fit:contain;background:#fff;border:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
.thumb svg{width:44px;height:60px}
.sale{display:inline-block;background:var(--dp-color-field-support);color:var(--dp-color-ink);border:2px solid var(--dp-color-ink);padding:0 6px;font-weight:var(--dp-typography-weight-action)}
.tag{display:inline-block;margin-left:8px;background:var(--dp-color-ink);color:var(--dp-color-on-field);font-size:var(--dp-typography-caption);font-weight:var(--dp-typography-weight-action);letter-spacing:.04em;text-transform:uppercase;padding:0 8px}
.arow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-bottom:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
.cats{display:grid;grid-template-columns:repeat(8,1fr);gap:3px;background:var(--dp-color-ink);border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}
button.cat{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;padding:14px;border:0;box-shadow:none;background:#fff;color:var(--dp-color-ink);font-size:var(--dp-typography-label);font-weight:var(--dp-typography-weight-action);text-align:left}
button.cat:hover:not(:disabled){background:var(--dp-color-field-support)}
button.cat:active:not(:disabled){transform:none;background:var(--dp-color-accent);color:var(--dp-color-on-field)}
button.cat[aria-pressed=true]{background:var(--dp-color-ink);color:var(--dp-color-on-field)}
button.cat[aria-pressed=true]:hover:not(:disabled){background:var(--dp-color-field);color:var(--dp-color-on-field)}
button.cat .cn{font-weight:800;text-align:right}
button.cat:focus-visible{outline:4px solid var(--dp-color-field);outline-offset:-4px}
button.cat[aria-pressed=true]:focus-visible{outline-color:var(--dp-color-field-support)}
tr.band td{background:var(--dp-color-field);color:var(--dp-color-on-field);border-bottom:0;padding:10px 12px;font-size:22px;font-weight:var(--dp-typography-weight-display);letter-spacing:-.02em}
tr.band .n{margin-left:14px;font-size:var(--dp-typography-label);font-weight:var(--dp-typography-weight-label);letter-spacing:.02em}
th button.sortbtn{padding:0;border:0;box-shadow:none;background:none;color:inherit;font:inherit;letter-spacing:inherit;text-transform:inherit;text-decoration:underline;text-underline-offset:4px;cursor:pointer}
th button.sortbtn:hover:not(:disabled){background:var(--dp-color-field-support);color:var(--dp-color-ink)}
th button.sortbtn:active:not(:disabled){transform:none}
th button.sortbtn:focus-visible{outline-color:var(--dp-color-field-support)}
@media (max-width:1050px){.cats{grid-template-columns:repeat(4,1fr)}.hero{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr)}button.circle{width:clamp(140px,20cqi,190px)}.ctl{gap:10px 18px}}
@media (max-width:720px){table{display:block;min-width:0}table tbody{display:block}
tr.hd{display:none}tr.band{display:block}tr.band td{display:block}tr.more{display:block}tr.more td{display:block;border-bottom:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
tr.item{display:grid;grid-template-columns:84px 1fr 1fr;column-gap:6px;padding:8px 0;border-bottom:var(--dp-border-rule-compact) solid var(--dp-color-ink)}
tr.item td{display:block;border:0;padding:4px 8px;background:transparent}
tr.item td[data-l]::before{content:attr(data-l);display:block;font-size:12px;font-weight:var(--dp-typography-weight-label);letter-spacing:.04em;text-transform:uppercase;color:#3d3d3d}
tr.item td:nth-child(1){grid-column:1;grid-row:1/span 4}
tr.item td:nth-child(2){grid-column:2/4;grid-row:1}
tr.item td:nth-child(3){grid-column:2;grid-row:2}tr.item td:nth-child(4){grid-column:3;grid-row:2}
tr.item td:nth-child(5){grid-column:2;grid-row:3}tr.item td:nth-child(6){grid-column:3;grid-row:3}
tr.item td:nth-child(7){grid-column:2/4;grid-row:4}
tr.best{background:var(--dp-color-field-support);box-shadow:inset 10px 0 0 var(--dp-color-ink)}.best td:first-child{box-shadow:none}
.cats{grid-template-columns:repeat(2,1fr)}.hero{grid-template-columns:1fr}.plane-text{border-right:0;border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}.plane-red{padding:36px var(--dp-spacing-gutter)}button.circle{width:176px}.fitgrid{grid-template-columns:1fr}.ctl{align-items:flex-start}}
.sizebox{display:inline-flex;flex-wrap:wrap;align-items:center;gap:6px;background:#fff;border:var(--dp-border-rule-compact) solid var(--dp-color-ink);padding:3px 6px;max-width:100%}
.sizebox:focus-within{outline:4px solid var(--dp-color-field);outline-offset:2px}
.sizebox input{border:0;width:130px;min-width:90px;padding:5px 4px;outline:none;background:transparent}
.sizebox input:focus-visible{outline:none}
button.stag{padding:1px 8px;min-height:30px;font-size:14px;border-width:2px;box-shadow:none;background:var(--dp-color-field);color:var(--dp-color-on-field);border-color:var(--dp-color-field)}
button.stag:hover:not(:disabled){background:var(--dp-color-ink);color:var(--dp-color-on-field)}
button.stag:active:not(:disabled){transform:none}
#sizetags{display:contents}
#ctl{position:sticky;top:0;z-index:30}
.chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:8px}
.chips .mut{margin-right:2px}
button.sz{padding:2px 9px;min-width:40px;min-height:32px;font-size:15px;font-weight:var(--dp-typography-weight-action);border-width:3px;box-shadow:none;background:#fff}
button.sz:active:not(:disabled){transform:none;background:var(--dp-color-accent);color:var(--dp-color-on-field)}
button.sz.out{color:#6b6b6b;border-color:#6b6b6b;text-decoration:line-through}
button.sz.on{background:var(--dp-color-field);color:var(--dp-color-on-field);border-color:var(--dp-color-field);text-decoration:none}
button.sz.more{border-style:dashed;font-size:14px}
tr.panel td{padding:0;background:var(--dp-color-ground);border-bottom:var(--dp-border-rule) solid var(--dp-color-ink)}
.pn{padding:16px var(--dp-spacing-gutter) 18px;box-shadow:inset 10px 0 0 var(--dp-color-field)}
.pnh{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.pnh h3{margin:0;font-size:20px;font-weight:var(--dp-typography-weight-display)}
button.x{padding:0 12px;font-size:24px;line-height:1.3;box-shadow:none;border-width:3px;flex:none}
button.x:active:not(:disabled){transform:none}
.mcells{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}
.mc{border:var(--dp-border-rule-compact) solid var(--dp-color-ink);background:#fff;padding:8px 10px}
.mc .k{font-size:12px;font-weight:var(--dp-typography-weight-label);letter-spacing:.06em;text-transform:uppercase}
.mc .v{font-size:26px;font-weight:var(--dp-typography-weight-display);line-height:1.1;letter-spacing:-.02em}
.mc .v small{font-size:13px;font-weight:var(--dp-typography-weight-label)}
.mc .st{font-size:13px;font-weight:var(--dp-typography-weight-label);min-height:1.3em}
.mc.in{border-color:#0a7a2f}.mc.in .st{color:#0a7a2f}
.mc.bad{border-color:var(--dp-color-accent)}.mc.bad .st{color:#c00000}
table.ct{min-width:0;width:100%;background:#fff;font-size:14px}
table.ct th{background:var(--dp-color-quiet);color:var(--dp-color-ink);text-transform:none;letter-spacing:0;text-align:right;padding:6px 8px;border:2px solid var(--dp-color-ink)}
table.ct td{padding:6px 8px;text-align:right;border:2px solid var(--dp-color-ink)}
table.ct th:first-child,table.ct td:first-child{text-align:left}
table.ct tr.hl td{background:var(--dp-color-field-support);font-weight:var(--dp-typography-weight-display)}
.nodata{border:3px dashed var(--dp-color-ink);background:#fff;padding:12px 14px;margin:12px 0}
.others{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px;font-size:var(--dp-typography-label)}
button.oth{padding:4px 10px;font-size:14px;border-width:3px;box-shadow:none}
button.oth[aria-pressed=true]{background:var(--dp-color-ink);color:var(--dp-color-on-field)}
button.oth:active:not(:disabled){transform:none}
.pn .ghost{margin-top:10px}
@media (max-width:720px){body.sheet-open{padding-bottom:62vh}
tr.panel{display:block}tr.panel>td{display:block;border:0}
.pn{position:fixed;left:0;right:0;bottom:0;max-height:78vh;overflow:auto;z-index:50;background:var(--dp-color-ground);border-top:var(--dp-border-rule) solid var(--dp-color-ink);box-shadow:0 -10px 0 rgba(0,0,0,.18);padding:14px 16px calc(18px + env(safe-area-inset-bottom,0px))}
table.ct{display:table}table.ct tbody{display:table-row-group}table.ct tr{display:table-row}table.ct td,table.ct th{display:table-cell}
.pn{max-height:62vh}.pnh h3{font-size:18px}
.mcells{grid-template-columns:repeat(3,1fr);gap:6px;margin:8px 0}.mc{padding:5px 7px}.mc .k{font-size:11px}.mc .v{font-size:19px}.mc .v small{font-size:11px}.mc .st{font-size:11px;min-height:0}
.ctw{overflow-x:auto}table.ct{font-size:12px}table.ct th,table.ct td{padding:4px 5px}
tr.item{scroll-margin-top:150px}
#ctl{padding:8px 14px;gap:6px 14px}#ctl select,#ctl input:not([type=checkbox]){padding:6px 8px;font-size:15px}}
</style>
<header class="hero"><section class="plane-text"><span class="tri" aria-hidden="true"></span><h1>Price Scout</h1><div class="bar"><input id="q" type="search" placeholder="Search a product, e.g. Momotaro 0705" autofocus aria-label="Search a product"></div><p class="mut">Searches: {{ sites|join(', ') }}</p></section><aside class="plane-red"><button id="go" class="circle" type="button"><span>Search</span><span class="arrow" aria-hidden="true">→</span></button></aside></header>
<nav class="cats" id="cats" aria-label="Garment categories"></nav>
<main>
<div class="ctl" id="ctl" hidden>
<label>Sort by <select id="sort"><option value="total">Price, low to high: est. total</option><option value="usd">Price, low to high: item price</option></select></label>
<label for="size">My sizes</label><span id="sizebox" class="sizebox"><span id="sizetags"></span><input id="size" list="sizes" placeholder="add size" autocomplete="off" aria-label="Add a size"></span><datalist id="sizes"></datalist>
<label><input type="checkbox" id="stock"> In stock only</label>
<label><input type="checkbox" id="sale"> On sale only</label></div>
<details id="fit" hidden><summary id="fitsum">Fit measurements (inches)</summary>
<div id="fitB"><p class="mut fitlab">Bottoms</p><div class="fitgrid fit"><label>Waist <input id="min_waist" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_waist" type="number" step="0.25" min="0" placeholder="max"></label><label>Thigh <input id="min_thigh" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_thigh" type="number" step="0.25" min="0" placeholder="max"></label><label>Front rise <input id="min_front_rise" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_front_rise" type="number" step="0.25" min="0" placeholder="max"></label><label>Back rise <input id="min_back_rise" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_back_rise" type="number" step="0.25" min="0" placeholder="max"></label><label>Knee <input id="min_knee" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_knee" type="number" step="0.25" min="0" placeholder="max"></label><label>Leg opening <input id="min_leg_opening" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_leg_opening" type="number" step="0.25" min="0" placeholder="max"></label></div></div>
<div id="fitT"><p class="mut fitlab">Tops and outerwear</p><div class="fitgrid fit"><label>Chest <input id="min_chest" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_chest" type="number" step="0.25" min="0" placeholder="max"></label><label>Shoulder <input id="min_shoulder" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_shoulder" type="number" step="0.25" min="0" placeholder="max"></label><label>Length <input id="min_length" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_length" type="number" step="0.25" min="0" placeholder="max"></label><label>Sleeve <input id="min_sleeve" type="number" step="0.25" min="0" placeholder="min"> – <input id="max_sleeve" type="number" step="0.25" min="0" placeholder="max"></label></div></div>
<p class="mut">Garment measurements read from each product’s size chart (its description, or its product page). Bottoms: waist, thigh, rises, knee, leg opening. Tops and outerwear: chest (flat, pit to pit), shoulder, length (back length), sleeve. Full circumferences are halved to flat widths and centimetres converted to inches. Listings without a readable chart (for example, a chart that is only an image) are hidden while a range is set, and a range for one group hides products from the other.</p></details>
<div id="out"></div>
<div class="ctl" id="abar" hidden>
<span>Alert me when est. total is at or below $</span><input id="amax" class="sm" type="number" min="0" step="1" placeholder="any">
<button id="mkalert" class="blue" type="button">Set alert</button><span class="mut" id="amsg">Uses your size and fit filters. Leave the price blank to be alerted when it's in stock.</span></div>
<details id="alerts" hidden><summary>Your price alerts</summary><div class="card"><div id="alist"></div>
<p class="mut">Checked every hour while this app is running. <button class="ghost" id="chk">Check now</button></p></div></details>
</main>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $=id=>document.getElementById(id);
const q=$('q'),go=$('go'),out=$('out'),sortSel=$('sort'),stockBox=$('stock'),saleBox=$('sale'),sizeIn=$('size'),amax=$('amax');
let DATA=null,LASTQ='';const OPEN=new Set();
let SEL=null,ALLROWS=false;const EXP=new Set(),LIM={};   // SEL = {i: offer index, size}: the one open measurements panel
const ls={get:k=>{try{return localStorage.getItem(k)||''}catch(e){return ''}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};

const money=(v,c)=>{try{return new Intl.NumberFormat('en-US',{style:'currency',currency:c}).format(v)}catch(e){return v.toFixed(2)+' '+c}};
const usdOrBlank=v=>v==null?'n/a':money(v,'USD');
const norm=s=>s.toLowerCase().replace(/\s+/g,'').replace(/^w(?=\d)/,'');
const sizeMatch=(label,want)=>[label].concat(label.split(/\s*[\/,]\s*|\s+x\s+/i)).some(p=>norm(p)===norm(want));
let SIZES=[];   // committed sizes (tags); whatever is still being typed counts too, so results filter as you type
const parseSizes=t=>String(t||'').split(/[,;]+/).map(x=>x.trim()).filter(Boolean);
const wantList=()=>SIZES.concat(parseSizes(sizeIn.value).filter(x=>!SIZES.some(y=>norm(y)===norm(x))));
const saveSizes=()=>{ls.set('ps_sizes',JSON.stringify(SIZES))};
try{SIZES=JSON.parse(ls.get('ps_sizes')||'[]')}catch(e){SIZES=[]}
if(!SIZES.length)SIZES=parseSizes(ls.get('ps_size'));
function drawSizes(){$('sizetags').innerHTML=SIZES.map((s,i)=>`<button type="button" class="stag" data-rm="${i}" aria-label="Remove size ${esc(s)}">${esc(s)} ×</button>`).join('')}
function commitSize(){parseSizes(sizeIn.value).forEach(x=>{if(!SIZES.some(y=>norm(y)===norm(x)))SIZES.push(x)});sizeIn.value='';saveSizes();drawSizes()}   // no re-render: the typed text already counted, and a re-draw here would swallow a click on the results
const hasSize=(o,w)=>!w.length||o.sizes.some(s=>s[1]&&w.some(x=>sizeMatch(s[0],x)));
const FIT_B=[['waist','Waist'],['thigh','Thigh'],['front_rise','Front rise'],['back_rise','Back rise'],['knee','Knee'],['leg_opening','Leg opening']];
const FIT_T=[['chest','Chest'],['shoulder','Shoulder'],['length','Length'],['sleeve','Sleeve']];
const FIT=FIT_B.concat(FIT_T);
const KEYS_BY={Bottoms:FIT_B,Tops:FIT_T,Outerwear:FIT_T};   // Outerwear shares the upper-body chart
const keysFor=o=>KEYS_BY[o.category]||FIT.filter(([k])=>o.measures.some(m=>m[k]!=null));
const num=id=>{const v=parseFloat($(id).value);return isNaN(v)?null:v};
const fitObj=()=>{const o={};visKeys().forEach(([k])=>{const lo=num('min_'+k),hi=num('max_'+k);if(lo!=null||hi!=null)o[k]=[lo,hi]});return o};
const fitRanges=()=>Object.entries(fitObj());
const fitSizes=(o,w,rs)=>o.measures.filter(m=>m.in_stock&&(!w.length||w.some(x=>sizeMatch(m.size,x)))&&rs.every(([k,[lo,hi]])=>m[k]!=null&&(lo==null||m[k]>=lo)&&(hi==null||m[k]<=hi))).map(m=>m.size);
const fitOk=(o,w,rs)=>rs.length?fitSizes(o,w,rs).length>0:hasSize(o,w);
const fitText=f=>{try{return Object.entries(JSON.parse(f||'{}')).map(([k,[lo,hi]])=>' · '+k.replace('_',' ')+' '+(lo!=null&&hi!=null?lo+'–'+hi:lo!=null?'≥ '+lo:'≤ '+hi)+'″').join('')}catch(e){return ''}};
const stripP=s=>String(s).replace(/\(.*?\)/g,'');
const sameSize=(a,b)=>sizeMatch(a,b)||sizeMatch(b,a)||norm(stripP(a))===norm(stripP(b));
const measureFor=(o,s)=>o.measures.find(m=>sameSize(m.size,s));
const fmt=v=>(+v).toFixed(2).replace(/\.?0+$/,'');
const rngText=([lo,hi])=>lo!=null&&hi!=null?lo+'–'+hi:lo!=null?'≥ '+lo:'≤ '+hi;
function chips(o,want,fits){
  const list=o.sizes,inS=list.filter(s=>s[1]);
  let base=want.length?list.filter(s=>want.some(x=>sameSize(s[0],x))):(fits&&fits.length?list.filter(s=>fits.some(f=>sameSize(f,s[0]))):inS.slice(0,8));
  if(!base.length)base=(inS.length?inS:list).slice(0,8);
  const vis=EXP.has(o._i)?list:base,hidden=list.length-base.length;
  const on=s=>SEL&&SEL.i===o._i&&sameSize(SEL.size,s);
  return '<div class="chips">'+(fits&&fits.length&&!EXP.has(o._i)?'<span class="mut">Fits:</span>':'')
    +vis.map(s=>`<button type="button" class="sz${s[1]?'':' out'}${on(s[0])?' on':''}" data-sz="${esc(s[0])}" data-oi="${o._i}" aria-expanded="${!!on(s[0])}" title="${s[1]?'In stock':'Sold out'}. Show measurements">${esc(s[0])}</button>`).join('')
    +(hidden>0?`<button type="button" class="sz more" data-exp="${o._i}">${EXP.has(o._i)?'fewer':'+'+hidden+' sizes'}</button>`:'')+'</div>';
}
function panel(l){
  const o=l.find(x=>x._i===SEL.i),size=SEL.size,fo=fitActive()?fitObj():{};
  const sz=o.sizes.find(s=>sameSize(s[0],size)),m=measureFor(o,size);
  const stock=sz?(sz[1]?'In stock':'Sold out'):'Not listed at this store';
  const link=`<a href="${esc(o.url)}" target="_blank" rel="noopener">Open the store’s page ↗</a>`;
  let inner;
  if(!o.measures.length){
    inner=`<div class="nodata"><b>No measurements we can read for this store.</b><div class="mut">Its size chart may be an image or a pop-up on the product page, so fit filters can’t be checked here. ${link}</div></div>`;
  }else{
    const cells=m?keysFor(o).filter(([k])=>m[k]!=null).map(([k,lab])=>{
      const r=fo[k];let c='',st='';
      if(r){const [lo,hi]=r,ok=(lo==null||m[k]>=lo)&&(hi==null||m[k]<=hi);c=ok?' in':' bad';st=ok?'✓ within your '+rngText(r):((lo!=null&&m[k]<lo?'↓ below':'↑ above')+' your '+rngText(r))}
      return `<div class="mc${c}"><div class="k">${lab}</div><div class="v">${fmt(m[k])}<small> in</small></div><div class="st">${st}</div></div>`}).join('')
      :`<p class="mut">This store’s chart has no row for size ${esc(size)}.</p>`;
    const rows=o.measures,keys=keysFor(o).filter(([k])=>rows.some(r=>r[k]!=null)),idx=m?rows.indexOf(m):-1;
    const win=(ALLROWS||rows.length<=3||idx<0)?rows:rows.slice(Math.min(Math.max(0,idx-1),rows.length-3),Math.min(Math.max(0,idx-1),rows.length-3)+3);
    inner=`<div class="mcells">${cells}</div><div class="ctw"><table class="ct"><thead><tr><th>Size</th>${keys.map(([,lab])=>`<th>${lab}</th>`).join('')}</tr></thead><tbody>`
      +win.map(r=>`<tr class="${r===m?'hl':''}"><td>${esc(r.size)}${r.in_stock?'':' (sold out)'}</td>${keys.map(([k])=>`<td>${r[k]!=null?fmt(r[k]):''}</td>`).join('')}</tr>`).join('')+'</tbody></table></div>'
      +(rows.length>3&&idx>=0?`<button type="button" class="ghost" data-allrows>${ALLROWS?'Show fewer sizes':'Show all '+rows.length+' sizes'}</button>`:'');
  }
  const oth=l.length>1?`<div class="others"><span>Same size at:</span>${l.map(x=>{const s2=x.sizes.find(s=>sameSize(s[0],size));return `<button type="button" class="oth" data-oth="${x._i}" aria-pressed="${x._i===o._i}">${esc(x.site)}${s2?(s2[1]?'':' (sold out)'):' (no '+esc(size)+')'}</button>`}).join('')}</div>`:'';
  return `<tr class="panel"><td colspan="8"><div class="pn" role="region" aria-label="Measurements for size ${esc(size)}"><div class="pnh"><div><h3>Size ${esc(size)} · ${esc(o.site)}</h3><div class="mut">${stock} · flat garment measurements in inches (cm converted)</div></div><button type="button" class="x" data-close aria-label="Close measurements">×</button></div>${inner}${oth}<p class="mut">From the store’s own size chart. Check the product page before buying. ${link}</p></div></td></tr>`;
}
const CATS=['Bottoms','Tops','Outerwear','Footwear','Bags','Accessories','Other'];
let CAT='All';
const showB=()=>CAT==='All'||CAT==='Bottoms',showT=()=>CAT==='All'||CAT==='Tops'||CAT==='Outerwear';
const fitActive=()=>showB()||showT();
const visKeys=()=>(showB()?FIT_B:[]).concat(showT()?FIT_T:[]);   // only the visible groups count
const rangesFor=o=>{const ks=(KEYS_BY[o.category]||[]).map(x=>x[0]);return fitRanges().filter(([k])=>ks.includes(k))};
const fitPass=(o,w)=>{const rs=rangesFor(o);return (fitRanges().length&&!rs.length)?false:fitOk(o,w,rs)};   // a range set for one group hides products of the other
const cmp=key=>(a,b)=>(a.sold_out-b.sold_out)||((a[key]==null?1e18:a[key])-(b[key]==null?1e18:b[key]));
function buildCats(){
  $('cats').innerHTML=['All'].concat(CATS).map(c=>`<button type="button" class="cat" data-cat="${c}" aria-pressed="false"><span class="nm">${c}</span><span class="cn"></span></button>`).join('');
}
function updateCats(base){
  $('cats').querySelectorAll('.cat').forEach(b=>{
    const c=b.dataset.cat,on=c===CAT;
    b.setAttribute('aria-pressed',on);
    b.querySelector('.nm').textContent=(on?'✓ ':'')+c;
    const n=!base?null:(DATA.browse&&c!==DATA.category)?null:(c==='All'?base.length:base.filter(o=>o.category===c).length);
    b.querySelector('.cn').textContent=n==null?'':n;
  });
}
function filtered(){
  const want=wantList(),ranges=fitActive()?fitRanges():[];
  const base=DATA.offers.filter(o=>!(stockBox.checked&&o.sold_out)&&!(saleBox.checked&&!o.was)&&fitPass(o,want));
  return {want,ranges,base};
}
function render(){
  if(!DATA)return;
  document.body.classList.toggle('sheet-open',!!SEL);
  const key=sortSel.value,{want,ranges,base}=filtered();
  const inCat=o=>CAT==='All'||o.category===CAT;
  $('fit').hidden=!fitActive();$('fitB').hidden=!showB();$('fitT').hidden=!showT();document.querySelectorAll('.fitlab').forEach(e=>e.hidden=!(showB()&&showT()));
  $('fitsum').textContent='Fit measurements (inches)'+(ranges.length?' · '+ranges.length+' active':'');
  updateCats(base);
  const offers=base.filter(inCat).sort(cmp(key));
  const lowTag=key==='total'?'Lowest total':'Lowest price';
  const row=(o,first,sub)=>{
    const item=o.usd==null?'n/a':(o.currency==='USD'?'':'≈ ')+money(o.usd,'USD');
    const local=money(o.price,o.currency)+(o.currency==='USD'?'':' '+o.currency)+(o.assumed?'*':'');
    const was=o.was?`<div class="mut"><s>${esc(money(o.was,o.currency))}</s> <span class="sale">−${Math.round((1-o.price/o.was)*100)}%</span></div>`:'';
    const rs=rangesFor(o),fits=rs.length?fitSizes(o,want,rs):null;
    const img=o.image?`<img class="thumb" src="${esc(o.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.opacity=.2">`:'<div class="thumb"></div>';
    return `<tr class="item ${first?'best':''}${sub?' sub':''}"><td>${img}</td>
      <td><a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.title)}</a>${first?`<span class="tag">${lowTag}</span>`:''}<div class="mut">${esc(o.site)}</div>${chips(o,want,fits)}</td>
      <td class="tot" data-l="Est. total">${usdOrBlank(o.total)}</td><td data-l="Item (USD)">${esc(item)}${was}</td><td data-l="Shipping">${usdOrBlank(o.ship)}</td><td data-l="Duty">${usdOrBlank(o.duty)}</td><td data-l="Ships from">${esc(o.ships_from_name||o.ships_from||'Unknown')}</td><td data-l="Local price">${esc(local)}</td></tr>`};
  const byGid=new Map();
  offers.forEach((o,i)=>{const g=o.gid||('x'+i);if(!byGid.has(g))byGid.set(g,[]);byGid.get(g).push(o)});   // offers are already sorted, so l[0] is the cheapest in-stock store for the current filters
  const prods=[...byGid.entries()],nProd=prods.length;
  const groups=(CAT==='All'?CATS:[CAT]).map(c=>[c,prods.filter(([,l])=>l[0].category===c)]).filter(g=>g[1].length);
  const lim=c=>CAT==='All'?5:(LIM[c]||20);   // under All each band shows its 5 cheapest products; in one category, 20 at a time
  const body=groups.map(([c,list])=>{
    const shown=list.slice(0,lim(c)),left=list.length-shown.length;
    return `<tr class="band"><td colspan="8">${esc(c)}<span class="n">${list.length} ${list.length===1?'product':'products'} · cheapest first</span></td></tr>`
    +shown.map(([gid,l],i)=>{
      const head=row(l[0],i===0&&!l[0].sold_out),n=l.length-1,pn=SEL&&l.some(o=>o._i===SEL.i)?panel(l):'';
      if(!n)return head+pn;
      const open=OPEN.has(gid);
      return head+pn+`<tr class="more"><td colspan="8"><button type="button" class="ghost" data-more="${esc(gid)}" aria-expanded="${open}">${open?'Hide':'See'} ${n} other ${n===1?'store':'stores'}</button></td></tr>`
        +(open?l.slice(1).map(o=>row(o,false,true)).join(''):'');
    }).join('')
    +(left>0?`<tr class="more"><td colspan="8">${CAT==='All'?`<button type="button" class="ghost" data-catgo="${esc(c)}">See all ${list.length} ${esc(c)} →</button>`:`<button type="button" class="ghost" data-lim="${esc(c)}">Show ${Math.min(20,left)} more (${left} left)</button>`}</td></tr>`:'');
  }).join('');
  const th=(k,label)=>`<th aria-sort="${key===k?'ascending':'none'}"><button type="button" class="sortbtn" data-sort="${k}" title="Sort cheapest first by ${esc(label)}">${label}${key===k?' ▲':''}</button></th>`;
  const assumed=DATA.offers.some(o=>o.assumed),scope=DATA.offers.filter(inCat);
  const noData=scope.filter(o=>ranges.length?!o.measures.length:!o.sizes.length).length;
  const shown=DATA.browse
    ?`${nProd} ${esc(DATA.category)} ${nProd===1?'product':'products'}, cheapest first${DATA.truncated?` (the ${DATA.offers.length} cheapest of ${DATA.total_matches.toLocaleString()}; type a product name to search them all)`:''}`
    :`${nProd} ${nProd===1?'product':'products'}, cheapest first${DATA.truncated?` (the ${DATA.offers.length} cheapest of ${DATA.total_matches.toLocaleString()})`:''}`;
  out.innerHTML=`<div class="card">`+(offers.length
    ?`<p class="mut">${shown}${want.length?' · size '+esc(want.join(' or '))+' in stock':''}${ranges.length?' · matching your fit measurements':''}${noData&&(want.length||ranges.length)?' · '+noData+' hidden: no '+(ranges.length?'measurement':'size')+' data':''}</p>
    <table><tr class="hd"><th></th><th>Product</th>${th('total','Est. total')}${th('usd','Item (USD)')}<th>Shipping</th><th>Duty</th><th>Ships from</th><th>Local price</th></tr>${body}</table>
    <p class="mut">Est. total = item + estimated shipping + import duty, assuming delivery to the US. Shipping and duty are flat estimates (edit them per store in sites.json); check each store at checkout. Sold-out items are listed last.</p>`
    :`<p>${DATA.browse?'No '+esc(DATA.category)+' listings match your filters.':'No matching results found.'}</p>`)
    +(assumed?'<p class="mut">* Currency not detected, so USD is assumed. Set "currency" for that store in sites.json.</p>':'')
    +(DATA.notes.length?`<p class="mut">${DATA.notes.map(esc).join(' · ')}</p>`:'')+'</div>';
}
function showHint(){
  DATA=null;$('ctl').hidden=true;$('fit').hidden=true;$('abar').hidden=true;updateCats(null);
  out.innerHTML='<div class="card"><p>Type a product above, or pick a category to browse its cheapest listings.</p></div>';
}
async function load(term,cat){
  go.disabled=true;out.innerHTML='<div class="card"><p class="mut">Searching stores…</p></div>';
  try{
    const r=await fetch('/api/search?q='+encodeURIComponent(term)+(cat?'&category='+encodeURIComponent(cat):''));
    if(!r.ok)throw new Error(r.status===429?'too many searches, please wait a minute and try again':'HTTP '+r.status);
    DATA=await r.json();LASTQ=term;
    DATA.offers.forEach((o,i)=>o._i=i);SEL=null;ALLROWS=false;EXP.clear();Object.keys(LIM).forEach(k=>delete LIM[k]);
    const set=new Set();DATA.offers.forEach(o=>o.sizes.forEach(s=>s[1]&&set.add(s[0])));
    $('sizes').innerHTML=[...set].sort((a,b)=>parseFloat(a)-parseFloat(b)||a.localeCompare(b)).slice(0,80).map(s=>`<option value="${esc(s)}">`).join('');
    const c=DATA.offers.filter(o=>(CAT==='All'||o.category===CAT)&&!o.sold_out&&o.total!=null&&fitPass(o,wantList())).map(o=>o.total);
    amax.value=c.length?Math.floor(Math.min(...c)*0.9):'';
    $('ctl').hidden=false;$('abar').hidden=!term;render();
  }catch(e){out.innerHTML='<div class="card"><p class="mut">Something went wrong: '+esc(e.message)+'</p></div>'}
  go.disabled=false;
}
function search(){
  const term=q.value.trim();
  if(term){load(term,null);return}
  if(CAT!=='All'){load('',CAT);return}
  showHint();
}
function newSearch(){if(q.value.trim())CAT='All';search()}   // a new search starts from All; picking a category afterwards narrows it
function pickCat(c){
  CAT=c;const term=q.value.trim();
  if(DATA&&!DATA.browse&&term&&term===LASTQ){render();return}            // same search: just filter what we have
  if(DATA&&DATA.browse&&!term&&DATA.category===c){render();return}      // already showing this category
  search();
}
async function loadAlerts(){
  try{
    const d=await (await fetch('/api/alerts')).json();
    $('alerts').hidden=!d.alerts.length;$('alerts').open=d.alerts.some(a=>a.notified_total!=null);
    $('alist').innerHTML=d.alerts.map(a=>{
      const cond=esc(a.query)+(a.category?' · '+esc(a.category):'')+(a.size?' · size '+esc(a.size):'')+esc(fitText(a.fit))+' · '+(a.max_total!=null?'≤ '+money(a.max_total,'USD'):'any price, in stock');
      const st=a.notified_total!=null
        ?`<b>Alert hit:</b> ${money(a.best_total,'USD')} at ${esc(a.best_site)} · <a href="${esc(a.best_url)}" target="_blank" rel="noopener">${esc(a.best_title)}</a>`
        :a.best_total!=null?`Watching. Best now ${money(a.best_total,'USD')} at ${esc(a.best_site)}`
        :a.checked?'Watching. Nothing in stock matches yet.':'Checking…';
      return `<div class="arow"><div>${cond}<div class="mut">${st}${a.checked?' · checked '+esc(new Date(a.checked*1000).toLocaleString()):''}</div></div>
        <button class="ghost" data-del="${a.id}">Delete</button></div>`}).join('');
    document.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{await fetch('/api/alerts/'+b.dataset.del,{method:'DELETE'});loadAlerts()});
  }catch(e){}
}
$('mkalert').onclick=async()=>{
  const body={query:LASTQ,category:CAT==='All'?'':CAT,size:wantList().join(','),fit:fitActive()?fitObj():{},max_total:amax.value};
  const r=await fetch('/api/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=r.ok?{}:await r.json().catch(()=>({}));
  $('amsg').textContent=r.ok?'Alert created. It is checked now, then every hour while this app is running.':(j.error||'Could not create that alert.');
  setTimeout(loadAlerts,2500);
};
$('chk').onclick=async()=>{$('chk').disabled=true;await fetch('/api/alerts/check',{method:'POST'});$('chk').disabled=false;loadAlerts()};
go.onclick=newSearch;sortSel.onchange=stockBox.onchange=saleBox.onchange=render;
$('cats').onclick=e=>{const b=e.target.closest('[data-cat]');if(b)pickCat(b.dataset.cat)};
out.onclick=e=>{
  const z=e.target.closest('[data-sz]');
  if(z){const oi=+z.dataset.oi,size=z.dataset.sz;SEL=(SEL&&SEL.i===oi&&sameSize(SEL.size,size))?null:{i:oi,size};ALLROWS=false;render();
    const n=[...out.querySelectorAll('[data-sz]')].find(b=>+b.dataset.oi===oi&&b.dataset.sz===size);if(n)n.focus({preventScroll:true});
    if(SEL){if(matchMedia('(min-width:721px)').matches){const p=out.querySelector('tr.panel');if(p)p.scrollIntoView({block:'nearest'})}
      else if(n){const r=n.closest('tr.item');if(r)r.scrollIntoView({block:'start'})}}   // phone: bring the product above the bottom sheet
    return}
  const x=e.target.closest('[data-exp]');if(x){const i=+x.dataset.exp;EXP.has(i)?EXP.delete(i):EXP.add(i);render();const n=out.querySelector('[data-exp="'+i+'"]');if(n)n.focus();return}
  const ot=e.target.closest('[data-oth]');if(ot){SEL={i:+ot.dataset.oth,size:SEL.size};render();const n=out.querySelector('[data-oth="'+ot.dataset.oth+'"]');if(n)n.focus();return}
  if(e.target.closest('[data-close]')){const i=SEL&&SEL.i;SEL=null;render();const n=out.querySelector('[data-oi="'+i+'"]');if(n)n.focus();return}
  if(e.target.closest('[data-allrows]')){ALLROWS=!ALLROWS;render();const n=out.querySelector('[data-allrows]');if(n)n.focus({preventScroll:true});return}
  const lm=e.target.closest('[data-lim]');if(lm){const c=lm.dataset.lim;LIM[c]=(LIM[c]||20)+20;render();return}
  const cg=e.target.closest('[data-catgo]');if(cg){pickCat(cg.dataset.catgo);window.scrollTo({top:out.offsetTop,behavior:'smooth'});return}
  const m=e.target.closest('[data-more]');if(m){const g=m.dataset.more;OPEN.has(g)?OPEN.delete(g):OPEN.add(g);render();const n=out.querySelector('[data-more="'+g+'"]');if(n)n.focus();return}const b=e.target.closest('[data-sort]');if(!b)return;sortSel.value=b.dataset.sort;render();const n=out.querySelector('[data-sort="'+b.dataset.sort+'"]');if(n)n.focus()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&SEL){SEL=null;render()}});
sizeIn.oninput=render;sizeIn.onchange=commitSize;
sizeIn.onkeydown=e=>{if(e.key==='Enter'||e.key===','){e.preventDefault();commitSize()}else if(e.key==='Backspace'&&!sizeIn.value&&SIZES.length){SIZES.pop();saveSizes();drawSizes();render()}};
$('sizebox').onclick=e=>{const b=e.target.closest('[data-rm]');if(b){SIZES.splice(+b.dataset.rm,1);saveSizes();drawSizes();render();sizeIn.focus()}};
document.querySelectorAll('.fit input').forEach(i=>i.oninput=render);
q.addEventListener('keydown',e=>{if(e.key==='Enter')newSearch()});
buildCats();updateCats(null);drawSizes();
loadAlerts();setInterval(loadAlerts,60000);
</script></html>"""

if __name__ == "__main__":
    threading.Thread(target=alert_loop, daemon=True).start()
    app.run(debug=False)
