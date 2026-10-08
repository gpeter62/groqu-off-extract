#!/usr/bin/env python3
"""Build an Open Food Facts extract of everyday products sold in Hungary for groQu.

Queries the OFF API v2 search endpoint politely (>= 10 s between requests,
back-off on 429/5xx) for products whose countries_tags include en:hungary,
in everyday categories and for a list of well-known brands, and writes
catalog/curated/off-hu-extract.json.

Each kept product gets an `app_category` mapped from its OFF categories_tags
(see APP_CATEGORY_RULES). Products that map to nothing are dropped, unless
they are generic snacks.

Data: (c) Open Food Facts contributors, https://world.openfoodfacts.org
License: ODbL 1.0 (database), DbCL 1.0 (contents).

Standard library only. Modelled on scripts/build-off-extract.py.

Usage:
  python scripts/build-off-hu-extract.py
  python scripts/build-off-hu-extract.py --resume cat:en:cheeses@2,brand:milka@1
  python scripts/build-off-hu-extract.py --max-requests 60
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

ENDPOINT = "https://world.openfoodfacts.org/api/v2/search"
USER_AGENT = "groQu/0.5 (build-off-hu-extract; gautier.p62@gmail.com)"
PAGE_SIZE = 100
SLEEP_S = 15.0  # >= 10 s between requests (OFF allows 10/min; 503s seen at 12 s)
RETRY_SLEEP_S = 120
MAX_REQUESTS = 120

FIELDS = [
    "code", "product_name", "product_name_hu", "product_name_en", "brands",
    "categories_tags", "nutriments", "nutrition_data_per", "allergens_tags",
    "traces_tags", "ingredients_text_hu", "serving_quantity", "serving_size",
    "quantity", "countries_tags", "last_modified_t",
]
# fields copied to the output as-is (ingredients_text_hu becomes has_ingredients)
OUT_FIELDS = [f for f in FIELDS if f not in ("ingredients_text_hu", "nutriments")]

COUNTRY = "en:hungary"

# (category tag, max pages). Parent tags are used where OFF's tree is reliable.
CATEGORY_QUERIES = [
    ("en:yogurts", 3),
    ("en:fermented-milk-products", 2),  # kefir and other cultured milks
    ("en:dairy-desserts", 2),           # puddings, túró desserts
    ("en:cheeses", 3),
    ("en:milk-drinks", 1),
    ("en:dairies", 3),                  # catches what the above miss
    ("en:salty-snacks", 3),
    ("en:chocolates", 3),
    ("en:confectioneries", 3),
    ("en:biscuits-and-cakes", 3),
    ("en:breakfast-cereals", 2),
    ("en:breads", 2),
    ("en:viennoiseries", 1),
    ("en:sodas", 2),
    ("en:fruit-juices", 2),
    ("en:prepared-meats", 3),
    ("en:meals", 2),
    ("en:spreads", 2),
    ("en:ice-creams-and-sorbets", 1),
]

# (brand tag, alternatives tried only if the first one finds nothing)
BRAND_QUERIES = [
    ("pottyos", ["pöttyös"]),
    ("turo-rudi", ["túró-rudi"]),
    ("milli", []),
    ("danone", []),
    ("muller", ["müller"]),
    ("mizo", []),
    ("sole", []),
    ("zott", []),
    ("chio", []),
    ("lay-s", ["lays"]),
    ("pringles", []),
    ("gyori", ["győri"]),
    ("balaton", []),
    ("sport", ["sport-szelet"]),
    ("boci", []),
    ("milka", []),
    ("medve", []),
    ("pick", []),
    ("gyulai", []),
    ("univer", []),
]
BRAND_MAX_PAGES = 2

# Ordered rules: the first rule with a matching tag wins. Specific before
# generic (e.g. ice cream before dairy, dairy desserts before sweets,
# alcoholic before beverages, generic en:dairies near the end).
APP_CATEGORY_RULES = [
    ("alcoholic_drinks", {
        "en:alcoholic-beverages", "en:beers", "en:wines", "en:spirits",
        "en:ciders", "en:liquors"}),
    ("sweets", {
        "en:ice-creams-and-sorbets", "en:ice-creams", "en:frozen-desserts",
        "en:ice-cream-bars", "en:ice-cream-tubs"}),
    ("dairy", {
        "en:yogurts", "en:fruit-yogurts", "en:fermented-milk-products",
        "en:kefirs", "en:kefir", "en:dairy-desserts", "en:puddings",
        "en:milk-puddings", "en:quark-desserts", "en:milk-drinks",
        "en:flavoured-milks", "en:chocolate-milks", "en:fermented-milk-drinks",
        "en:dairy-drinks"}),
    ("cheese", {
        "en:cheeses", "en:cottage-cheeses", "en:quarks", "en:quark",
        "en:fresh-cheeses", "en:processed-cheese", "en:cheese-spreads"}),
    ("sweets", {
        "en:chocolates", "en:confectioneries", "en:candies",
        "en:chocolate-candies", "en:bonbons", "en:chocolate-spreads",
        "en:hazelnut-spreads", "en:sweet-spreads", "en:jams", "en:honeys",
        "en:chocolate-bars", "en:gummies", "en:chewing-gum"}),
    ("bakery_sweets", {
        "en:biscuits-and-cakes", "en:biscuits", "en:cakes", "en:wafers",
        "en:pastries", "en:viennoiseries", "en:sweet-pastries-and-pies",
        "en:croissants", "en:cookies"}),
    ("cereals", {
        "en:breakfast-cereals", "en:mueslis", "en:granolas",
        "en:cereal-flakes", "en:oat-flakes", "en:cereal-bars"}),
    ("bread", {
        "en:breads", "en:sliced-breads", "en:white-breads",
        "en:wholemeal-breads", "en:rye-breads", "en:crispbreads",
        "en:rusks", "en:special-breads"}),
    ("snacks", {
        "en:salty-snacks", "en:chips-and-fries", "en:crisps",
        "en:potato-crisps", "en:appetizers", "en:crackers", "en:popcorn",
        "en:pretzels", "en:salted-snacks", "en:nuts", "en:puffed-salty-snacks",
        "en:extruded-snacks"}),
    ("beverages", {
        "en:beverages", "en:sodas", "en:carbonated-drinks", "en:fruit-juices",
        "en:juices-and-nectars", "en:nectars", "en:waters", "en:iced-teas",
        "en:energy-drinks", "en:syrups", "en:soft-drinks"}),
    ("cold_cuts", {
        "en:prepared-meats", "en:sausages", "en:hams", "en:salamis",
        "en:cold-cuts", "en:bacons", "en:pates", "en:meat-pastes",
        "en:frankfurters"}),
    ("dishes", {
        "en:meals", "en:prepared-meals", "en:soups", "en:pizzas",
        "en:canned-meals", "en:frozen-ready-made-meals", "en:pasta-dishes",
        "en:sandwiches", "en:salads"}),
    ("fats_oils", {
        "en:fats", "en:vegetable-oils", "en:butters", "en:margarines",
        "en:vegetable-fats", "en:animal-fats", "en:lards"}),
    ("sauces", {
        "en:sauces", "en:condiments", "en:ketchup", "en:mayonnaises",
        "en:mustards", "en:dressings", "en:salted-spreads", "en:spreads",
        "en:dips"}),
    ("fruit", {
        "en:fruits", "en:fresh-fruits", "en:dried-fruits", "en:compotes",
        "en:canned-fruits", "en:fruits-based-foods"}),
    ("vegetables", {
        "en:vegetables", "en:fresh-vegetables", "en:canned-vegetables",
        "en:frozen-vegetables", "en:vegetables-based-foods", "en:legumes",
        "en:pickles"}),
    ("dairy", {
        "en:dairies", "en:milks", "en:creams", "en:sour-creams",
        "en:dairy-substitutes"}),
    # last resort: generic snacks stay as snacks, everything else is dropped
    ("snacks", {"en:snacks", "en:sweet-snacks"}),
]

CODE_RE = re.compile(r"^\d{8,14}$")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "catalog", "curated", "off-hu-extract.json")


class Fetcher:
    def __init__(self):
        self.requests = 0
        self.last = 0.0
        self.failures = []
        self.interval = SLEEP_S

    def _wait(self):
        delta = time.monotonic() - self.last
        if self.last and delta < self.interval:
            time.sleep(self.interval - delta)

    def get(self, params, label):
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        for attempt in (1, 2):
            if self.requests >= MAX_REQUESTS:
                raise RuntimeError("request cap reached")
            self._wait()
            self.requests += 1
            self.last = time.monotonic()
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or 500 <= e.code < 600
                print(f"  HTTP {e.code} for {label} (attempt {attempt})", flush=True)
                if retryable:
                    # back off: slow down subsequent requests too
                    self.interval = min(self.interval * 1.5, 45.0)
                if retryable and attempt == 1:
                    time.sleep(RETRY_SLEEP_S)
                    continue
                self.failures.append(f"{label}: HTTP {e.code}")
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                print(f"  error for {label} (attempt {attempt}): {e}", flush=True)
                if attempt == 1:
                    time.sleep(RETRY_SLEEP_S)
                    continue
                self.failures.append(f"{label}: {e}")
                return None
        return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kcal_of(n):
    kcal = num(n.get("energy-kcal_100g"))
    if kcal is None:
        kj = num(n.get("energy-kj_100g"))
        if kj is None:
            kj = num(n.get("energy_100g"))  # OFF's generic energy is in kJ
        if kj is not None:
            kcal = kj / 4.184
    return kcal


def app_category(cats):
    for app_cat, tags in APP_CATEGORY_RULES:
        if cats & tags:
            return app_cat
    return None


def slim_nutriments(n):
    out = {}
    for k, v in n.items():
        if k.endswith("_100g") or k.endswith("_serving") or k in ("energy-kcal", "energy-kj"):
            out[k] = v
    return out


def convert(p):
    """Return the output record, or None if the product is not kept."""
    code = str(p.get("code") or "").strip()
    if not CODE_RE.match(code):
        return None
    name = " ".join(str(p.get(k) or "").strip()
                    for k in ("product_name", "product_name_hu", "product_name_en")).strip()
    if not name:
        return None
    if COUNTRY not in (p.get("countries_tags") or []):
        return None
    n = p.get("nutriments") or {}
    kcal = kcal_of(n)
    if kcal is None or not (0 <= kcal <= 900):
        return None
    for key in ("proteins_100g", "fat_100g", "carbohydrates_100g"):
        v = num(n.get(key))
        if v is not None and not (0 <= v <= 100):
            return None
    cat = app_category(set(p.get("categories_tags") or []))
    if cat is None:
        return None
    rec = {k: p[k] for k in OUT_FIELDS if k in p}
    rec["code"] = code
    rec["nutriments"] = slim_nutriments(n)
    rec["has_ingredients"] = bool(str(p.get("ingredients_text_hu") or "").strip())
    rec["app_category"] = cat
    return rec


def run_query(fetcher, base_params, label, max_pages, kept, start_page=1):
    """Returns the OFF count of the first page fetched (None on failure)."""
    page = start_page
    first_count = None
    while True:
        params = dict(base_params)
        params.update({
            "countries_tags": COUNTRY, "page_size": PAGE_SIZE, "page": page,
            "fields": ",".join(FIELDS)})
        data = fetcher.get(params, f"{label} p{page}")
        if data is None:
            return first_count
        if first_count is None:
            first_count = data.get("count")
        products = data.get("products") or []
        n_kept = 0
        for p in products:
            rec = convert(p)
            if rec and rec["code"] not in kept:
                kept[rec["code"]] = rec
                n_kept += 1
        print(f"  {label} p{page}: {len(products)} products, +{n_kept} kept "
              f"(count={data.get('count')}, total kept={len(kept)}, req={fetcher.requests})",
              flush=True)
        if len(products) < PAGE_SIZE:
            return first_count
        if page >= max_pages:
            print(f"  {label}: page cap {max_pages} reached", flush=True)
            fetcher.failures.append(
                f"{label}: truncated at {page} pages (count={data.get('count')})")
            return first_count
        page += 1


def all_queries():
    """label -> (params, max_pages, alternative brand tags)"""
    q = {}
    for c, pages in CATEGORY_QUERIES:
        q[f"cat:{c}"] = ({"categories_tags": c}, pages, [])
    for b, alts in BRAND_QUERIES:
        q[f"brand:{b}"] = ({"brands_tags": b}, BRAND_MAX_PAGES, alts)
    return q


def parse_args(argv):
    """--resume cat:en:cheeses@2,brand:milka@1 merges into the existing output
    file and runs only the listed queries, starting at the given page.
    --max-requests N overrides the cap."""
    resume, max_req = None, None
    i = 0
    while i < len(argv):
        if argv[i] == "--resume":
            resume = []
            for item in argv[i + 1].split(","):
                label, _, page = item.strip().rpartition("@")
                resume.append((label, int(page)))
            i += 2
        elif argv[i] == "--max-requests":
            max_req = int(argv[i + 1])
            i += 2
        else:
            raise SystemExit(f"unknown argument: {argv[i]}")
    return resume, max_req


def write_output(kept):
    out = {
        "source": {
            "provider": "Open Food Facts",
            "license": "ODbL 1.0 (database), DbCL 1.0 (contents)",
            "citation": "© Open Food Facts contributors, https://world.openfoodfacts.org",
            "retrievedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "api": "v2 search",
            "dataset": "groQu Hungarian everyday products extract",
        },
        "products": [kept[c] for c in sorted(kept)],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    os.replace(tmp, OUT_PATH)


def main():
    global MAX_REQUESTS
    resume, max_req = parse_args(sys.argv[1:])
    if max_req is not None:
        MAX_REQUESTS = max_req
    fetcher = Fetcher()
    kept = {}
    if resume is not None and os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            for p in json.load(f).get("products", []):
                kept[p["code"]] = p
        print(f"Resume: loaded {len(kept)} existing products", flush=True)
    queries = all_queries()
    plan = [(label, 1) for label in queries] if resume is None else resume
    fail_streak = 0
    try:
        for i, (label, start) in enumerate(plan):
            if label not in queries:
                raise SystemExit(f"unknown query label: {label}")
            params, max_pages, alts = queries[label]
            count = run_query(fetcher, params, label, max_pages, kept, start)
            fail_streak = fail_streak + 1 if count is None else 0
            if fail_streak >= 3:
                rest = [f"{l}@{p}" for l, p in plan[i + 1 - fail_streak:]]
                write_output(kept)
                print("Stopped: OFF keeps failing; rerun later with --resume "
                      + ",".join(rest), flush=True)
                fetcher.failures.append("aborted after repeated failures")
                break
            if count == 0:
                for alt in alts:
                    print(f"  {label}: no results, trying brand tag {alt}", flush=True)
                    count = run_query(fetcher, {"brands_tags": alt}, f"brand:{alt}",
                                      max_pages, kept)
                    if count:
                        break
            write_output(kept)  # checkpoint, so an interrupted run can --resume
    except RuntimeError as e:
        print(f"Stopped: {e}", flush=True)
        fetcher.failures.append(str(e))

    write_output(kept)
    print(f"Done: {fetcher.requests} requests, {len(kept)} products -> {OUT_PATH}")
    for cat, n in sorted(Counter(p["app_category"] for p in kept.values()).items()):
        print(f"  {cat}: {n}")
    if fetcher.failures:
        print("Failures:")
        for x in fetcher.failures:
            print("  " + x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
