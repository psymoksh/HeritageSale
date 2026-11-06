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

## 4. Change a store's settings

1. Open `sites.json`.
2. Add any of these to a store's entry: `origin` (country it ships from, for example `"JP"`), `currency`, `shipping_usd`, `free_shipping_over_usd`, `duty_pct`, `duties_included`.
3. Save and restart the site (step 2 above).
4. To add a store, add `{"name": "...", "base_url": "https://..."}` to the list.

## 5. Turn on email alerts (optional)

1. Set these before running the site: `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASS`, `ALERT_FROM`.
2. Optionally set `ALERT_INTERVAL_MIN` (default 60 minutes).
3. Run `python app.py`, search for a product, and use "Set alert". Without SMTP settings, alerts still show inside the app.

## 6. Check your changes before committing

1. Compile check: `python3 -W error -m py_compile app.py check_stores.py`.
2. Start the site and try a search at a wide window and a phone-width window (under 720 px).
3. Open `../price-scout-prototype.html` and confirm it still matches the app if the page layout changed.
4. Add a line to the change log below.

## Change log

- **Initial commit.** Multi-store search with local currency and USD; estimated shipping and duty; size filter; sale awareness; product images; fit-measurement filters (waist, thigh, front rise, back rise, knee, leg opening); price alerts; garment categories; cheapest-first ordering; one row per product with "See N other stores"; "Ships from" country column; Bauhaus styling with a phone card layout; `check_stores.py` health check.
