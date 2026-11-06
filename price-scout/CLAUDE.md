# Price Scout

Flask site that searches a list of heritage / Japanese denim retailers for the best price on a product, with local currency + USD, estimated shipping and import duty, size and fit filters, price alerts, sale awareness and product images. Project: HeritageSale.

## Run
```
pip install -r requirements.txt   # flask, requests, beautifulsoup4
python app.py                     # http://127.0.0.1:5000
```
Optional env vars for alert emails: `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASS`, `ALERT_FROM`, `ALERT_INTERVAL_MIN` (60).

## Layout
- `app.py`: everything (scraping, currency, categories, alerts, the page template `PAGE`).
- `sites.json`: the 25 stores, each `{name, base_url}`. Optional per-store overrides:
  `origin`, `currency`, `shipping_usd`, `free_shipping_over_usd`, `duty_pct`, `duties_included`,
  `type: "html"` with CSS selectors (`item`, `title`, `price`, `link`, optional `image`, `sizes`, `size_soldout_class`) and `search_url` for non-Shopify stores (e.g. Self Edge, which runs OpenCart). `check_stores.py` searches "jeans" for html stores when no query is given. `search_encoding` (e.g. `euc-jp` for Celluloid's Color Me Shop) encodes the query; non-$ prices take the first number (Japanese "30,000円(税込33,000円)" -> 30,000, the export / pre-tax price).
  `type: "square"` + `square_owner_id` + `square_site_id` (Brass Tacks) reads Square Online's storefront JSON at `/app/store/api/v28/editor/users/{owner}/sites/{site}/products`, reshaped like a Shopify product. Find the owner id in the shop page's network requests (`/app/website/cms/api/v1/users/{owner}/...`) and the site id as `catalogSiteId` in the page HTML.
- `alerts.db`: SQLite, created on first run.

## Endpoints
`/` page · `GET /api/search?q=&category=` · `/api/alerts` (GET/POST/DELETE) · `/api/alerts/check`

## How it works
- Shopify stores: `/products.json` catalog (cached 1h), `/meta.json` for currency. Other stores: search-page scrape (selectors, then JSON-LD, then a heuristic). robots.txt is checked.
- Currency: open.er-api.com, fallback Frankfurter, cached 12h.
- Est. total = item + shipping + duty, converted to USD. Defaults: duty 16.6% on non-US origins (cotton jeans); the US $800 de minimis is suspended. Flat shipping estimates; edit per store in `sites.json`.
- Fit data (waist, thigh, front/back rise, knee, leg opening): parsed from size charts in product HTML (either table orientation, text charts, cm to inches, fractions), with a product-page fallback (cached 6h, max 10 page fetches per store).
- Categories: Bottoms, Tops, Outerwear, Footwear, Bags, Accessories, Other. Assigned by ordered regex rules in `_RULES` (first match wins): product_type, then title, then tags, then denim/fit words fall back to Bottoms. Edit `_RULES` / `_STRIP` to fix misfiled items.
- Ordering: every list and every category band is cheapest first. Default key is est. total; item price is selectable (dropdown or column header). Sold-out items always last.
- Browse mode: empty search + a category shows the cheapest 200 (`BROWSE_CAP`); searches cap at 600 (`SEARCH_CAP`).
- Grouping: the same product at several stores shows once, at the cheapest store that has the shopper's size in stock, with a "See N other stores" link. Server (`assign_groups` / `group_key`) gives each offer a `gid`: name match (vendor + model, ignoring case, punctuation, word order and filler words; wash/colour words are kept so a different wash stays separate) OR a shared Shopify barcode. The client groups the already-filtered, sorted offers, so the size / fit / in-stock filters decide which store headlines. Matching is conservative: when unsure, listings stay separate. Grouping runs after the 200/600 cap.
- Ships from: each offer carries `ships_from` (ISO code) and `ships_from_name`, shown as a "Ships from" column. `sites.json` `origin` wins (code or English name); otherwise a Shopify store's `/meta.json` country is used (the shop's registered country, a proxy for where it ships from). It also drives shipping/duty: non-US origin gets the international shipping estimate and duty, even if the store prices in USD. Unknown shows "Unknown" and falls back to currency (USD = domestic).
- Fit filters apply only under All and Bottoms.
- Alerts: hourly background thread; can be scoped by category, size and fit ranges.

## Design
Bauhaus design system, style only: `--dp-*` tokens, Host Grotesk, flat red/blue/yellow planes, square corners, circular search button with hard offset shadow. Under 720px the results table becomes stacked cards (`tr.item`, `data-l` labels).

## Real-store check
`python check_stores.py [query] [-s "store name"]` hits every store for real and prints reachability, listings found, currency, ships-from, sizes/images coverage and fit-measure coverage on a sample of Bottoms; full detail goes to `store_report.json`. The cloud sandbox cannot reach the stores (network allowlist), so run it on a normal internet connection.

## Testing
Mock `requests.get`; Playwright with `/usr/bin/google-chrome --no-sandbox` for UI checks at 1280px and 390px; `node --check` on the page JS; `python3 -W error -m py_compile app.py`.

## Prototype
`../price-scout-prototype.html` is a standalone clickable demo with made-up data. Keep it in sync with the `PAGE` markup and CSS when the UI changes.
