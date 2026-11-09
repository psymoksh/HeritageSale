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
3. Type a product (for example "momotaro 0705") and press Search. Results must contain exactly what you typed. Or pick a category with the box empty to browse its cheapest items. Click a size button on any result to see that size's measurements.
4. Stop the site with Ctrl+C.

## 3. Test against the real stores

The cloud sandbox cannot reach the stores, so do this on your own computer.

1. Test one store first: `python check_stores.py -s "Self Edge"`.
2. Test all 24: `python check_stores.py` (add `-c Tops` to check tops' chest/shoulder/length/sleeve instead of jeans). It takes a minute or two.
3. Read the table: `OK` says whether the store was reachable, `LISTINGS` how many items were found, `SHIPS FROM` the detected country, `SIZES`/`IMG` data coverage, `MEAS` how many sampled jeans had readable measurements.
4. Open `store_report.json` for the full detail, including the reason each failed store failed.
5. Share the table to get failing stores fixed.

### 3b. Find out why fit measurements are missing for a store

To look at one product page in detail (for example a store whose size table is not being read): `python3 diagnose_fit.py --dump "https://store.com/products/some-jeans"`. It saves `page_dump.html` and prints whether the table is in the raw page, in Japanese, inside a script or in an iframe. Attach `page_dump.html` and the printed output to the chat.

1. Run `python3 diagnose_fit.py` (all stores) or `python3 diagnose_fit.py -s "Tendrel"` (one store). It takes a minute or two.
2. Each store gets a one-line verdict, for example "chart appears to be an image" or "measurement words present but parser missed it".
3. Attach the saved `fit_diagnosis.txt` to the chat so the parser can be taught that store's chart layout. For each "parser missed" product the file includes the page address and the raw HTML around the measurement words, so no separate page dump is needed.

### 3d. Find out why one product shows no sizes or measurements

1. Run `python3 inspect_product.py "https://store.com/products/the-product"`.
2. Read the lines it prints: the store currency and the price (catalog price the app uses next to the price the page shows visitors), the option names, the size buttons the app builds from them, the category, and whether a size chart was found in the description or on the page and matched to a size.
3. Paste the output when asking for a fix.

### 3c. Find out why a search finds nothing at one store

Run `python3 explain_search.py "momotaro 0705 tapered" -s "Red Cast"` (your search text, and part of the store's name). It loads that store's real catalog and prints, for each word you typed, how many products contain it as a whole word, how many match every word (what the site shows), and the closest misses with the word that was missing. Use it when a store seems to be missing results.

## 4. Change a store's settings

1. Open `sites.json`.
2. Add any of these to a store's entry: `origin` (country it ships from, for example `"JP"`), `currency`, `shipping_usd`, `free_shipping_over_usd`, `duty_pct`, `duties_included`.
3. Save and restart the site (step 2 above).
4. To add a store, add `{"name": "...", "base_url": "https://..."}` to the list.

## 5. Use price alerts

1. Run `python app.py` and search for a product. Set your size and fit filters if you want them applied.
2. Add one or more sizes under "My sizes" if you want them applied. Enter a target price (or leave it blank to be alerted when it is in stock) and press "Set alert".
3. Alerts are checked now and then every hour while the app is running. Hits show under "Your price alerts"; press "Check now" to check immediately.
4. Email alerts are removed for now. To change the check interval, set `ALERT_INTERVAL_MIN` (default 60 minutes) before running.

## 6. Put it on the internet safely

1. Install the pinned packages: `pip install -r requirements.txt` (exact versions, so nothing changes under you). To upgrade later: `pip install -U -r requirements.in`, run the checks in section 7, then regenerate the pins (`pip freeze` for the packages listed there and their dependencies) into `requirements.txt`. Check them for known problems with `pip install pip-audit && pip-audit -r requirements.txt`.
2. Run it with a production server, not `python app.py`: for example `BACKGROUND=1 gunicorn -w 1 app:app` behind HTTPS. Use one worker, because the alert timer, the store warm-up and the visitor counters live in the app process; for several workers set `LIMITER_STORAGE=redis://...`. `BACKGROUND=1` starts the warm-up and the alert checker (`python app.py` does this by itself).
3. If a host or proxy sits in front of the app (Render, Fly, nginx), set `TRUST_PROXY=1`. Without it every visitor looks like the same address and shares one limit.
4. Starting limits per visitor: search 20 per minute, create alert 10 per hour, "Check now" 5 per hour, size-chart loading 600 per hour, everything else 300 per hour, 20 alerts per browser. Change any of them without editing code: `LIMIT_SEARCH`, `LIMIT_ALERT_CREATE`, `LIMIT_ALERT_CHECK`, `LIMIT_FIT`, `LIMIT_DEFAULT` (for example `LIMIT_SEARCH="30 per minute"`) and `MAX_ALERTS_PER_OWNER`.
5. After a week of real traffic, look for "429" lines in your host's logs. Many from real people means loosen that limit; none at all and heavy load means tighten it.
6. Alerts live in a small SQLite file, `alerts.db`. Most hosts wipe the app's disk on every deploy or restart, which would delete everyone's alerts. Attach a persistent disk (Render "Disk", Fly "Volume", Railway "Volume") mounted at, say, `/data`, and set `ALERTS_DB=/data/alerts.db` and `CACHE_DIR=/data/cache`. Back the file up now and then by copying it. Without a disk the site works, but alerts reset on each deploy.
7. Alerts belong to the browser that created them (a private cookie). Clearing cookies or switching browsers means you no longer see them. Alerts made before this change have no owner and are not shown (they stay in `alerts.db`).
8. Privacy page: the site serves `/privacy` and links it in the footer. Set `CONTACT_EMAIL=you@example.com` so it shows a contact line, and read the text once to be sure it matches what you do. It is a plain-language description, not legal advice.
9. Security headers are sent automatically (content security policy, no framing, no content sniffing, HTTPS-only when served over HTTPS). Check the live site at securityheaders.com after you deploy. If you add a script, a font or an image source from another website, the policy blocks it until you allow it in `csp()` in `app.py`.

## 6b. Speed: warm cache and lazy size charts

1. When the app starts it reads the saved store catalogs from the `cache/` folder (so a restart is instant), then refreshes every store in the background every 30 minutes (`WARM_EVERY_MIN`). A search normally never waits on the stores. The very first run on a new machine still has to download everything once.
2. If a store is down, the last good copy keeps being used (for up to 24 hours) and the app retries every 5 minutes.
3. Size charts that live on product pages are no longer fetched while you wait. The list appears first with a note "reading N more size charts…", and fit filters and size panels fill in within a few seconds. Price alerts still wait for full data.
4. To clear the cache, stop the app and delete the `cache/` folder. `CACHE_DIR` moves it elsewhere (set it to a persistent disk on a host that wipes files on restart).

## 7. Check your changes before committing

1. Compile check: `python3 -W error -m py_compile app.py check_stores.py`.
2. Start the site and try a search at a wide window and a phone-width window (under 720 px).
3. Open `../price-scout-prototype.html` and confirm it still matches the app if the page layout changed.
4. Add a line to the change log below.

## Change log

- **inspect_product.py shows prices.** It now prints the store's currency, the catalog price the app uses and the price the page itself shows visitors, to find why a store's prices look wrong.

- **Reggieshop tops: centimetres written as "72cm" and "Body width".** Two chart-reading fixes found on a real Reggieshop shirt page. (1) A chart whose cells read `72cm` (unit attached to the number) was not recognised as centimetres, so 72 was treated as 72 inches; cm is now detected in both tables and text charts. Any store that writes sizes like `81cm` was affected, and its measurements will now be correct. (2) "Body width" now means chest. Japanese 着丈 / 身幅 / 肩幅 / 袖丈 charts already worked. `inspect_product.py URL` shows what the app sees for one product.

- **Alerts database location.** `ALERTS_DB` sets where `alerts.db` is stored (its folder is created if missing), so it can sit on a host's persistent disk. Default is unchanged (next to `app.py`).

- **Faster first search, and launch hardening.** *Speed:* store catalogs are now refreshed in the background every 30 minutes and saved to a `cache/` folder, so a search no longer waits on 25 stores and a restart is instant; an hour-old catalog is shown at once while a fresh one loads, and a failing store keeps its last good copy. Converting 8,000 products per store happens once per catalog instead of once per search. Size charts that live on product pages load after the results appear (`POST /api/fit`, ten at a time) instead of holding up the search; the page shows "reading N more size charts…". *Security:* every response now carries a content security policy (scripts and styles only with a per-page random nonce), no-framing, no-sniffing, referrer and permissions headers, HSTS over HTTPS, and no caching of API replies; the one inline event handler was removed. Listing links and images must be http(s): the server drops anything else and the page double-checks. The size-chart loader only fetches pages on our own stores' hosts. Dependencies are pinned to exact versions in `requirements.txt` (`requirements.in` lists the direct ones). A privacy page at `/privacy` is linked in the footer. See sections 6 and 6b for the settings.

- **Several sizes at once.** "My sizes" now takes more than one size: type a size and press Enter (or comma, or pick from the list) and it becomes a tag; click a tag's × or press Backspace in the empty box to remove it. A product shows if any chosen size is in stock, its size buttons show only your sizes, and what you are still typing filters live. The chosen sizes are remembered in this browser, and a price alert saves all of them (any one in stock triggers it). The prototype matches.

- **New searches start from All.** Pressing Search (or Enter) with text in the box resets the category to All, so results are never narrowed by a category left over from the last search. Pick a category afterwards to narrow them. Pressing Search with an empty box still browses the category you picked.

- **Tops and outerwear size charts.** Tops and outerwear now get their own measurements: chest (flat, pit to pit), shoulder, length (back length) and sleeve. The parser reads them from the same chart layouts as bottoms (tables either way round, text lines, slash-separated lists, single-size notes; English and Japanese labels such as 身幅, 肩幅, 着丈, 袖丈), converts cm to inches, handles fractions, and halves a full chest circumference (over 30 in) to the flat width, like the waist. Letter sizes (S, M, L, XL, "Small") in text charts are recognised. The fit panel has a second group of min/max boxes for them: under All you see both groups, under Tops or Outerwear only the upper-body group, under Bottoms only the bottoms group. A range applies to its own category only, and while any range is set a product needs a range for its own category (so a chest range under All hides bottoms, and setting both shows bottoms that fit the waist range and tops that fit the chest range). The size panel shows the four upper-body cards and chart rows for a top. Price alerts accept the new ranges. `check_stores.py` and `diagnose_fit.py` take `-c Tops` (or `Outerwear`) to check them live. The chart row's size is now the product's own size label, so "Small" in a chart matches an "S" variant. Also fixed: clicking a size button right after typing in "My sizes" no longer loses the click.

- **Search explainer.** `explain_search.py "query" -s "store"` shows why a store returns few or no results for a search (per-word counts and near misses). Added after Red Cast looked empty although its catalog loads (1,877 listings).

- **Size chart panel, shorter pages, exact search.**
  1. *Size buttons.* Each product shows its in-stock sizes as buttons (at most 8; "+N sizes" reveals the rest, sold-out ones crossed out). With "My size" set, only that size shows; with fit ranges set, only fitting sizes show.
  2. *Click a size* and a panel opens under that product: the six measurements for that size (green "within your range" / red above or below, when a fit range is set), the three nearest rows of the store's chart ("Show all N sizes" for the rest), and "Same size at:" buttons to compare other stores. Stores whose chart can't be read show a box saying so, with a link to the product page.
  3. *One panel at a time.* Opening another size closes the first; Escape or × closes it. On a phone (under 720 px) the panel is a bottom sheet over the list, so the page does not get longer.
  4. *Shorter lists.* Under All, each category shows its 5 cheapest products with "See all N". Inside one category, 20 products at a time ("Show 20 more"). The filter bar (sort, size, in stock, sale) stays pinned at the top. On the same 120-product test data the All view went from 16,800 px to 6,700 px tall on desktop and from 42,700 px to 15,600 px on a phone.
  5. *Exact search.* Every word you type must appear in the title as a whole word, in any order, ignoring case and punctuation ("tapered momotaro" finds "Momotaro ... Tapered"; "momotaro0705" finds "Momotaro 0705"; "ron" does not find "Iron Heart"). A typed word containing a digit may be the start of a model number ("0705" finds "0705SP"). The old rule accepted a title containing 60% of the words anywhere, including parts of words. Price alerts use the same rule.
  6. The prototype was updated to match.

- **Two new size-chart layouts and a stricter detector.** Read from the first full `fit_diagnosis.txt`: (1) BEARS TOKYO writes its chart as slash-separated text (a header line `Size/waist/front rise/...` then one line per size such as `30inc/76cm/30cm/...`, with decimal commas); the parser now reads it, centimetres converted to inches. (2) Reserve Supply gives one size only (`Front rise: 11 1/4", Knee: 18", ... Measurements based on size 32`); the parser records those values for that one size. Chart sizes written like `36(S)` now match variants labelled `36` or `36(S)`. `diagnose_fit.py` now counts a chart only when two measurement words sit among six or more numbers, because ordinary sentences ("high rise", "room through the thigh") and a menu item ("Hem Orders") caused most of the earlier "parser missed" verdicts. Findings: Tendrel, Tate + Yoko, Corlection, Withered Fig have no chart in the page text (prose only); Iron Shop and HeritageQualityGoods (and Withered Fig's "posted measurements") publish charts as photos; these need OCR or are left without fit data.

- **Fit diagnosis shows its evidence.** For every product where `diagnose_fit.py` finds measurement words but the parser reads nothing, `fit_diagnosis.txt` now says whether the words are in the catalog description or on the page, and includes the raw HTML around them (tags included) so the chart's layout is visible. The console also prints the first such product URL per store. Reason: two of the eight "parser missed" stores (BEARS TOKYO, Withered Fig) turned out to have no readable chart on the sampled page.

- **Japanese size charts.** The parser now reads common Japanese labels (ウエスト waist, 股上 rise, ワタリ thigh, 膝 knee, 裾幅 leg opening), treats charts with Japanese text as centimetres when no unit is printed, and halves a waist given as a full circumference (over 26 in) to match the flat measurement the filters use. Added for Reggieshop; confirmed on a live product page (5 sizes read, values converted correctly). `diagnose_fit.py --dump URL` added to inspect a single product page.

- **Catalog cap raised to 8,000 products per store** (was 5,000, which cut off Blue Owl and Blue Seven). Set `CATALOG_CAP` to change it. A store with 8,000 products now takes up to 32 requests to load, once an hour, so the first search after a restart can be slower.
- **Fit diagnosis tool made stricter.** It now needs two different measurement words (waist, thigh, rise, knee...) before it says a chart is present; the first version matched pieces of ordinary words like "enterprise" and over-reported. Re-run `python3 diagnose_fit.py` for trustworthy verdicts.

- **Fit diagnosis tool.** `diagnose_fit.py` checks a few in-stock jeans per store and reports whether a size chart exists and whether the parser read it. First live run: all 25 stores returned listings; fit measurements were readable at only 6 stores (Blue Owl, Hinoya, Okayama Denim, Denimio, Red Cast, James Dant). Known: Blue Owl and Blue Seven hit the 5,000-product catalog cap; Celluloid shows no sizes.

- **Abuse protection.** Per-visitor rate limits (see section 6); alerts are private to the browser that made them (cookie token), at most 20 each; the public "check all alerts" endpoint now only checks the caller's own alerts and is limited to 5 per hour; alert lists no longer include any email data; `sites.json` is found relative to `app.py` so the app starts from any folder. New dependency: `flask-limiter`.

- **Store fixes (merged from live testing).** robots.txt now fetched properly; Self Edge, Celluloid and Brass Tacks read from their own page/catalog formats; Reggieshop added (25 stores); Japanese size option recognised. Re-run `python check_stores.py` to see current coverage.

- **Removed email alerts.** No email field, no SMTP code or settings. Alerts are in-app only. Old alert databases still work; the unused email column is ignored. Reason: the public-launch security review found email would let anyone send mail to any address; bring it back with confirmation and unsubscribe.

- **Initial commit.** Multi-store search with local currency and USD; estimated shipping and duty; size filter; sale awareness; product images; fit-measurement filters (waist, thigh, front rise, back rise, knee, leg opening); price alerts; garment categories; cheapest-first ordering; one row per product with "See N other stores"; "Ships from" country column; Bauhaus styling with a phone card layout; `check_stores.py` health check.
