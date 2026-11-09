# Price Scout

Flask site that searches a list of heritage / Japanese denim retailers for the best price on a product, with local currency + USD, estimated shipping and import duty, size and fit filters, price alerts, sale awareness and product images. Project: HeritageSale.

## Run
```
pip install -r requirements.txt   # flask, requests, beautifulsoup4
python app.py                     # http://127.0.0.1:5000
```
Optional env var: `ALERT_INTERVAL_MIN` (60). Email alerts were removed for now (see below).

## Layout
- `app.py`: everything (scraping, currency, categories, alerts, the page template `PAGE`).
- `sites.json`: the 25 stores, each `{name, base_url}`. Optional per-store overrides:
  `origin`, `currency`, `shipping_usd`, `free_shipping_over_usd`, `duty_pct`, `duties_included`,
  `type: "html"` with CSS selectors (`item`, `title`, `price`, `link`, optional `image`, `sizes`, `size_soldout_class`) and `search_url` for non-Shopify stores (e.g. Self Edge, which runs OpenCart). `check_stores.py` searches "jeans" for html stores when no query is given. `search_encoding` (e.g. `euc-jp` for Celluloid's Color Me Shop) encodes the query; non-$ prices take the first number (Japanese "30,000円(税込33,000円)" -> 30,000, the export / pre-tax price).
  `type: "square"` + `square_owner_id` + `square_site_id` (Brass Tacks) reads Square Online's storefront JSON at `/app/store/api/v28/editor/users/{owner}/sites/{site}/products`, reshaped like a Shopify product. Find the owner id in the shop page's network requests (`/app/website/cms/api/v1/users/{owner}/...`) and the site id as `catalogSiteId` in the page HTML.
- `alerts.db`: SQLite, created on first run. `ALERTS_DB=/path/alerts.db` moves it (put it, and `CACHE_DIR`, on the host's persistent disk or alerts are lost on each deploy).

## Endpoints
`/` page · `GET /api/search?q=&category=` (20/min) · `POST /api/fit` (body `{urls:[≤10]}`, 600/h; only our stores' product pages) · `/privacy` · `/api/alerts` (GET/POST 10/h/DELETE, owner-scoped) · `POST /api/alerts/check` (owner's alerts only, 5/h)

## How it works
- Shopify stores: `/products.json` catalog (cached 1h, `cached_catalog`: stale-while-revalidate, an older copy up to 24h is served at once while a fresh one loads; a failed refresh keeps the last good copy and retries in 5 min; one fetch per store at a time), `/meta.json` for currency. Converted listings are memoised per catalog (`shopify_found`). Other stores: search-page scrape (selectors, then JSON-LD, then a heuristic). robots.txt is checked.
- Currency: open.er-api.com, fallback Frankfurter, cached 12h.
- Est. total = item + shipping + duty, converted to USD. Defaults: duty 16.6% on non-US origins (cotton jeans); the US $800 de minimis is suspended. Flat shipping estimates; edit per store in `sites.json`.
- Fit data (waist, thigh, front/back rise, knee, leg opening): parsed from size charts in product HTML (either table orientation, text charts, cm to inches, fractions), with a product-page fallback (cached 6h, max 10 page fetches per store).
- Categories: Bottoms, Tops, Outerwear, Footwear, Bags, Accessories, Other. Assigned by ordered regex rules in `_RULES` (first match wins): product_type, then title, then tags, then denim/fit words fall back to Bottoms. Edit `_RULES` / `_STRIP` to fix misfiled items.
- Ordering: every list and every category band is cheapest first. Default key is est. total; item price is selectable (dropdown or column header). Sold-out items always last.
- Browse mode: empty search + a category shows the cheapest 200 (`BROWSE_CAP`); searches cap at 600 (`SEARCH_CAP`).
- Grouping: the same product at several stores shows once, at the cheapest store that has the shopper's size in stock, with a "See N other stores" link. Server (`assign_groups` / `group_key`) gives each offer a `gid`: name match (vendor + model, ignoring case, punctuation, word order and filler words; wash/colour words are kept so a different wash stays separate) OR a shared Shopify barcode. The client groups the already-filtered, sorted offers, so the size / fit / in-stock filters decide which store headlines. Matching is conservative: when unsure, listings stay separate. Grouping runs after the 200/600 cap.
- Ships from: each offer carries `ships_from` (ISO code) and `ships_from_name`, shown as a "Ships from" column. `sites.json` `origin` wins (code or English name); otherwise a Shopify store's `/meta.json` country is used (the shop's registered country, a proxy for where it ships from). It also drives shipping/duty: non-US origin gets the international shipping estimate and duty, even if the store prices in USD. Unknown shows "Unknown" and falls back to currency (USD = domestic).
- Search matching (`matches`): every typed word must be a whole word in the title, any order, ignoring case and punctuation; a typed word with a digit may prefix a model number ("0705" finds "0705SP"); a space-less query of 5+ characters also matches ("momotaro0705"). No fuzzy or partial-word matching. Alerts share it.
- Page layout (space): each row shows in-stock size buttons (`chips`, max 8; `EXP` reveals all); clicking one sets `SEL = {i, size}` and renders one `tr.panel` (`panel(l)`) after that product's head row: six measurement cards (status vs fit ranges), nearest 3 chart rows (`ALLROWS` for all), "Same size at" store buttons, or a "no readable chart" box linking to the product page. Offers get a client index `_i` in `load()`. Under 720px the panel is a fixed bottom sheet (`.pn`; `body.sheet-open` adds scroll room); its CSS must stay in the LAST media block. Under All each category band shows 5 products ("See all N" switches category); inside a category 20 at a time (`LIM`). `#ctl` filter bar is sticky.
- My sizes: `SIZES` (tags, saved in localStorage `ps_sizes`) plus whatever is typed in the box, via `wantList()`; all client matching helpers (`hasSize`, `fitSizes`, `chips`) take a list and match ANY size. The server accepts `size` as a comma list (`want_sizes`; alerts store up to 60 characters).
- New search (`newSearch`, the Search button / Enter with text) resets `CAT` to 'All'; picking a category afterwards narrows the same results, and an empty box + Search keeps browsing the picked category.
- Fit groups: Bottoms = waist, thigh, front/back rise, knee, leg opening; Tops and Outerwear = chest (flat pit to pit), shoulder, length (back length), sleeve (`FIT_B` / `FIT_T`, server `BOTTOM_KEYS` / `TOP_KEYS` / `KEYS_BY_CATEGORY`). Parser: `measure_key` (English + `_JP_KEYS`: 身幅 肩幅 着丈 袖丈), same table / text / slash / single-size layouts, cm to inches, chest > 30 in treated as circumference and halved. Ranges apply per category (`rangesFor`, `fitPass`, server `fit_ok`); with any range set a product needs a range for its own category. Panel keys by category (`keysFor`). `measure_rows` returns the variant's own size label (chart "Small" matches variant "S"). `check_stores.py` / `diagnose_fit.py` take `-c Tops|Outerwear`.
- Fit filters show under All (both groups), Bottoms (bottoms group) and Tops / Outerwear (upper-body group).
- Alerts: hourly background thread; can be scoped by category, size and fit ranges. In-app only: the email feature was removed before launch (an open email field lets anyone send mail to any address). If it returns, it needs address validation, double opt-in, unsubscribe and per-address limits. The old `email` DB column is kept for compatibility and ignored.

## Speed
- Warm-up: `start_background()` (called by `python app.py`, or set `BACKGROUND=1` under gunicorn with ONE worker) loads the disk cache, then `prewarm_loop` refreshes every store's catalog, `/meta.json` and the FX rates every `WARM_EVERY_MIN` (30) minutes, so a search normally never waits on stores. Catalogs are saved gzip-JSON in `cache/` (`CACHE_DIR`, git-ignored) and reloaded at startup (up to 24h old), so a restart is instant.
- Lazy fit charts: `/api/search` runs `run_search(..., lazy=True)`: charts in the catalog description are used, charts already cached from product pages are used, the rest are flagged `fit_pending` (max `MAX_PAGE_FETCHES` = 10 per store) and the page asks `POST /api/fit` in batches of 10 (3 at a time) after the list is shown, then re-renders (status "reading N more size charts…"; pending listings are not counted as "hidden: no measurement data"). Alerts call `run_search` with `lazy=False`, so they see full data.

## Abuse protection
- Flask-Limiter, per visitor IP, in-memory by default (`LIMITER_STORAGE=redis://...` for several workers). Starting limits: search 20/min, create alert 10/h, check-now 5/h, everything 300/h. Env overrides: `LIMIT_SEARCH`, `LIMIT_ALERT_CREATE`, `LIMIT_ALERT_CHECK`, `LIMIT_DEFAULT`. Over the limit returns 429 JSON with `Retry-After`; the page shows a "wait a minute" message.
- Set `TRUST_PROXY=<number of proxies>` behind a host/proxy so the limiter sees real visitor IPs.
- Alert ownership: random `ps_owner` cookie (HttpOnly, SameSite=Lax); list/delete/check only touch that owner's alerts; max 20 each (`MAX_ALERTS_PER_OWNER`). Pre-existing alerts with no owner are hidden. The hourly background job still checks all alerts.
- Security headers on every response (`security_headers`): CSP (page: `default-src 'none'`, scripts and styles only with a per-response nonce, `style-src-attr 'unsafe-inline'`, fonts from Google Fonts, images any https/http, `connect-src 'self'`, `frame-ancestors 'none'`; JSON: `default-src 'none'`), nosniff, X-Frame-Options DENY, Referrer-Policy, Permissions-Policy, COOP, HSTS when HTTPS, `Cache-Control: no-store` on `/api/`. No inline event handlers or `eval` in the page (the CSP forbids them); tests that use Playwright `wait_for_function` with a string must use polling instead.
- Links: the server drops any listing whose URL is not http(s) with a host (`safe_http`) and nulls non-http(s) image URLs; the page also runs every href / src through `safeUrl`. `/api/fit` only fetches product pages on configured store hosts.
- Dependencies pinned in `requirements.txt` (direct + transitive, exact versions); `requirements.in` holds the loose direct list for upgrades.
- Privacy page at `/privacy` (footer link); optional `CONTACT_EMAIL` env adds a contact line. It describes: no accounts, the `ps_owner` cookie, `ps_sizes` local storage, in-memory IP rate limiting, Google Fonts, store sites. Update it if data collection changes.
- Still open before launch: a real contact address in `CONTACT_EMAIL`, HTTPS host with `TRUST_PROXY`, a read of the privacy text by the owner (it is plain-language, not legal advice).

## Design
Bauhaus design system, style only: `--dp-*` tokens, Host Grotesk, flat red/blue/yellow planes, square corners, circular search button with hard offset shadow. Under 720px the results table becomes stacked cards (`tr.item`, `data-l` labels).

## Real-store check
`python check_stores.py [query] [-s "store name"] [-c Tops]` hits every store for real and prints reachability, listings found, currency, ships-from, sizes/images coverage and fit-measure coverage on a sample of Bottoms; full detail goes to `store_report.json`. The cloud sandbox cannot reach the stores (network allowlist), so run it on a normal internet connection.

## Fit diagnosis
`python3 diagnose_fit.py [-s "store"]` samples in-stock Bottoms per Shopify store and reports, from the catalog description and the product page, whether a size chart is present and whether `parse_charts` read it (verdicts: charts read / words present but parser missed / image chart / script-loaded / none). Writes `fit_diagnosis.txt`. Live baseline (check_stores, 2026-11-06): 25/25 stores return listings; readable fit data at Blue Owl, Hinoya, Okayama Denim, Denimio, Red Cast, James Dant only. `get_catalog` caps at `CATALOG_CAP` = 8,000 products per store (env override; 250 per request, 32 requests at most). The old 5,000 cap truncated Blue Owl and Blue Seven. A chart counts as present only with at least two distinct measurement words (English or Japanese). For "parser missed" products `fit_diagnosis.txt` also records where the words were found (description or page) and the raw HTML around them. Fit-data status (2026-11-06 diagnosis): readable at Blue Owl, Hinoya, Okayama, Denimio, Rodeo Japan, Son of a Stag, Red Cast, James Dant, dc4, Reggieshop; BEARS TOKYO (slash-separated text chart, `_slash_charts`) and Reserve Supply (single base size, `BASE_SIZE`) added; Tendrel, Tate + Yoko, Corlection, Withered Fig are prose only; Iron Shop, HeritageQualityGoods use photo charts; Blue in Green loads a widget; Mildblend, Blue Seven, Manready publish none. Chart labels like `36(S)` match variants `36` or `36(S)`. The detector needs two measurement words among 6+ numbers. `python3 diagnose_fit.py --dump URL` inspects one product page (saves `page_dump.html`). Parser (confirmed on a live Reggieshop page): Japanese labels (`_JP_KEYS`), Japanese charts assumed cm unless inches are printed, and waist > 26 in is treated as circumference and halved (filters use flat waist).

## One product
`python3 inspect_product.py PRODUCT_URL` fetches a Shopify product's `.js` and page and prints the option names, size buttons (`variant_sizes`), category, chart sizes found in the description and on the page, and the rows matched to size buttons; use it when one product shows no sizes or measurements.

## Search explainer
`python3 explain_search.py "query" -s "store"` loads one Shopify store's catalog and shows, per typed word, how many products contain it as a whole word, how many match every word (what the site shows), and near misses with the missing word. Live check 2026-11-06: Red Cast Heritage loads 1,877 listings (EUR, ships from Spain, 10/10 measurements), so empty results there come from the search wording, not the store.

## Testing
Mock `requests.get` (or patch `app.run_search` with generated offers for UI tests); Playwright with `/usr/bin/google-chrome --no-sandbox` for UI checks at 1280px and 390px; `node --check` on the page JS; `python3 -W error -m py_compile app.py`.

## Prototype
`../price-scout-prototype.html` is a standalone clickable demo with made-up data. Keep it in sync with the `PAGE` markup and CSS when the UI changes.
