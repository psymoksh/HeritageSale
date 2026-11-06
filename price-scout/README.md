# Price Scout

Search 24 heritage and Japanese denim stores at once. Results are ranked by estimated landed cost (item + shipping + import duty, in USD), grouped by garment category, with one row per product at the cheapest in-stock store for your size.

Design and behavior notes live in `CLAUDE.md`. This file is the step-by-step guide. **Update the "Change log" at the bottom every time code changes.**

## 1. Set up (first time)

1. Install Python 3.9 or newer. Check with `python3 --version` (Windows: `python --version`).
2. Open a terminal in this folder (`price-scout`).
3. Install the packages: `pip install -r requirements.txt`.

## 2. Run the website

1. In the terminal, run `python app.py`.
2. Open http://127.0.0.1:5000 in your browser.
3. Type a product (for example "momotaro") and press Search, or pick a category with the box empty to browse its cheapest items.
4. Stop the site with Ctrl+C.

## 3. Test against the real stores

The cloud sandbox cannot reach the stores, so do this on your own computer.

1. Test one store first: `python check_stores.py -s "Self Edge"`.
2. Test all 24: `python check_stores.py`. It takes a minute or two.
3. Read the table: `OK` says whether the store was reachable, `LISTINGS` how many items were found, `SHIPS FROM` the detected country, `SIZES`/`IMG` data coverage, `MEAS` how many sampled jeans had readable measurements.
4. Open `store_report.json` for the full detail, including the reason each failed store failed.
5. Share the table to get failing stores fixed.

### 3b. Find out why fit measurements are missing for a store

1. Run `python3 diagnose_fit.py` (all stores) or `python3 diagnose_fit.py -s "Tendrel"` (one store). It takes a minute or two.
2. Each store gets a one-line verdict, for example "chart appears to be an image" or "measurement words present but parser missed it".
3. Attach the saved `fit_diagnosis.txt` to the chat so the parser can be taught that store's chart layout.

## 4. Change a store's settings

1. Open `sites.json`.
2. Add any of these to a store's entry: `origin` (country it ships from, for example `"JP"`), `currency`, `shipping_usd`, `free_shipping_over_usd`, `duty_pct`, `duties_included`.
3. Save and restart the site (step 2 above).
4. To add a store, add `{"name": "...", "base_url": "https://..."}` to the list.

## 5. Use price alerts

1. Run `python app.py` and search for a product. Set your size and fit filters if you want them applied.
2. Enter a target price (or leave it blank to be alerted when it is in stock) and press "Set alert".
3. Alerts are checked now and then every hour while the app is running. Hits show under "Your price alerts"; press "Check now" to check immediately.
4. Email alerts are removed for now. To change the check interval, set `ALERT_INTERVAL_MIN` (default 60 minutes) before running.

## 6. Put it on the internet safely

1. Install the new package first: `pip install -r requirements.txt` (adds `flask-limiter`).
2. Run it with a production server, not `python app.py`: for example `gunicorn -w 1 app:app` behind HTTPS. Use one worker, because the alert timer and the visitor counters live in the app process; for several workers set `LIMITER_STORAGE=redis://...`.
3. If a host or proxy sits in front of the app (Render, Fly, nginx), set `TRUST_PROXY=1`. Without it every visitor looks like the same address and shares one limit.
4. Starting limits per visitor: search 20 per minute, create alert 10 per hour, "Check now" 5 per hour, everything 300 per hour, 20 alerts per browser. Change any of them without editing code: `LIMIT_SEARCH`, `LIMIT_ALERT_CREATE`, `LIMIT_ALERT_CHECK`, `LIMIT_DEFAULT` (for example `LIMIT_SEARCH="30 per minute"`) and `MAX_ALERTS_PER_OWNER`.
5. After a week of real traffic, look for "429" lines in your host's logs. Many from real people means loosen that limit; none at all and heavy load means tighten it.
6. Alerts belong to the browser that created them (a private cookie). Clearing cookies or switching browsers means you no longer see them. Alerts made before this change have no owner and are not shown (they stay in `alerts.db`).

## 7. Check your changes before committing

1. Compile check: `python3 -W error -m py_compile app.py check_stores.py`.
2. Start the site and try a search at a wide window and a phone-width window (under 720 px).
3. Open `../price-scout-prototype.html` and confirm it still matches the app if the page layout changed.
4. Add a line to the change log below.

## Change log

- **Fit diagnosis tool.** `diagnose_fit.py` checks a few in-stock jeans per store and reports whether a size chart exists and whether the parser read it. First live run: all 25 stores returned listings; fit measurements were readable at only 6 stores (Blue Owl, Hinoya, Okayama Denim, Denimio, Red Cast, James Dant). Known: Blue Owl and Blue Seven hit the 5,000-product catalog cap; Celluloid shows no sizes.

- **Abuse protection.** Per-visitor rate limits (see section 6); alerts are private to the browser that made them (cookie token), at most 20 each; the public "check all alerts" endpoint now only checks the caller's own alerts and is limited to 5 per hour; alert lists no longer include any email data; `sites.json` is found relative to `app.py` so the app starts from any folder. New dependency: `flask-limiter`.

- **Store fixes (merged from live testing).** robots.txt now fetched properly; Self Edge, Celluloid and Brass Tacks read from their own page/catalog formats; Reggieshop added (25 stores); Japanese size option recognised. Re-run `python check_stores.py` to see current coverage.

- **Removed email alerts.** No email field, no SMTP code or settings. Alerts are in-app only. Old alert databases still work; the unused email column is ignored. Reason: the public-launch security review found email would let anyone send mail to any address; bring it back with confirmation and unsubscribe.

- **Initial commit.** Multi-store search with local currency and USD; estimated shipping and duty; size filter; sale awareness; product images; fit-measurement filters (waist, thigh, front rise, back rise, knee, leg opening); price alerts; garment categories; cheapest-first ordering; one row per product with "See N other stores"; "Ships from" country column; Bauhaus styling with a phone card layout; `check_stores.py` health check.
