#!/usr/bin/env python3
"""Build an Open Food Facts extract of sports-nutrition products for groQu.

Queries the OFF API v2 search endpoint politely (<= 10 req/min) and writes
catalog/curated/off-sports-extract.json.

Data: (c) Open Food Facts contributors, https://world.openfoodfacts.org
License: ODbL 1.0 (database), DbCL 1.0 (contents).

Standard library only.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENDPOINT = "https://world.openfoodfacts.org/api/v2/search"
USER_AGENT = "groQu/0.3 (build-off-extract; gautier.p62@gmail.com)"
PAGE_SIZE = 100
SLEEP_S = 15.0  # OFF allows 10/min; we stay well below (503s seen at 6.5 s)
RETRY_SLEEP_S = 60
MAX_REQUESTS = 120
MAX_PAGES_PER_QUERY = 10  # keep one huge brand from eating the request budget

FIELDS = [
    "code", "product_name", "product_name_hu", "product_name_en", "brands",
    "brands_tags", "categories_tags", "countries_tags", "quantity",
    "product_quantity", "product_quantity_unit", "serving_size",
    "serving_quantity", "nutriments", "allergens_tags", "traces_tags",
    "ingredients_text", "ingredients_text_hu", "last_modified_t",
    "nutrition_data_per",
]

BRANDS = [
    "biotech-usa", "biotechusa", "scitec-nutrition", "gymbeam", "myprotein",
    "nutriversum", "optimum-nutrition", "weider", "olimp", "amix", "nutrend",
    "bodylab", "prozis", "powerbar", "multipower", "ehrmann",
]
PROTEIN_ONLY_BRANDS = {"ehrmann"}

HU_CATEGORIES = ["en:protein-powders", "en:protein-bars", "en:sports-nutrition"]

SPORTS_CATEGORIES = {
    "en:sports-nutrition", "en:protein-powders", "en:protein-bars",
    "en:dietary-supplements", "en:protein-shakes", "en:energy-drinks",
    "en:isotonic-drinks", "en:energy-bars", "en:whey-proteins",
}
NAME_KEYWORDS = re.compile(
    r"protein|whey|fehérje|bar|szelet|shake|gainer|casein|isolate", re.I)
PROTEIN_KEYWORDS = re.compile(r"protein|whey|fehérje", re.I)
SUPPLEMENT_KEYWORDS = re.compile(
    r"capsule|tablet|kapszula|tabletta|creatine|kreatin|vitamin", re.I)
CODE_RE = re.compile(r"^\d{8,14}$")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "catalog", "curated", "off-sports-extract.json")


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
        if kj is not None:
            kcal = kj / 4.184
    return kcal


def keep(p, from_brand, protein_only):
    code = str(p.get("code") or "").strip()
    if not CODE_RE.match(code):
        return False
    name = " ".join(str(p.get(k) or "").strip()
                    for k in ("product_name", "product_name_hu", "product_name_en")).strip()
    if not name:
        return False
    n = p.get("nutriments") or {}
    kcal = kcal_of(n)
    prot = num(n.get("proteins_100g"))
    if kcal is None or prot is None:
        return False
    # sanity
    if not (0 <= kcal <= 900):
        return False
    for key in ("proteins_100g", "fat_100g", "carbohydrates_100g"):
        v = num(n.get(key))
        if v is not None and not (0 <= v <= 100):
            return False
    cats = set(p.get("categories_tags") or [])
    if protein_only and not PROTEIN_KEYWORDS.search(name):
        return False
    is_sport = bool(cats & SPORTS_CATEGORIES) or (from_brand and NAME_KEYWORDS.search(name))
    if not is_sport:
        return False
    if kcal < 50 and (SUPPLEMENT_KEYWORDS.search(name)
                      or any(SUPPLEMENT_KEYWORDS.search(c) for c in cats)):
        return False
    return True


def run_query(fetcher, base_params, label, from_brand, protein_only, kept, start_page=1):
    page = start_page
    while True:
        params = dict(base_params)
        params.update({"page_size": PAGE_SIZE, "page": page, "fields": ",".join(FIELDS)})
        data = fetcher.get(params, f"{label} p{page}")
        if data is None:
            return
        products = data.get("products") or []
        n_kept = 0
        for p in products:
            if keep(p, from_brand, protein_only):
                code = str(p["code"]).strip()
                if code not in kept:
                    kept[code] = {k: p[k] for k in FIELDS if k in p}
                    kept[code]["code"] = code
                    n_kept += 1
        print(f"  {label} p{page}: {len(products)} products, +{n_kept} kept "
              f"(count={data.get('count')}, total kept={len(kept)}, req={fetcher.requests})",
              flush=True)
        if len(products) < PAGE_SIZE:
            return
        if page >= MAX_PAGES_PER_QUERY:
            print(f"  {label}: page cap {MAX_PAGES_PER_QUERY} reached", flush=True)
            fetcher.failures.append(f"{label}: truncated at {page} pages (count={data.get('count')})")
            return
        page += 1


def parse_args(argv):
    """--resume brand:gymbeam@1,brand:myprotein@3,cat:en:protein-bars@1
    merges into the existing output file and runs only the listed queries,
    starting at the given page. --max-requests N overrides the cap."""
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


def all_queries():
    # Hungarian category queries first: they matter most for the app
    for c in HU_CATEGORIES:
        yield (f"cat:{c}", {"categories_tags": c, "countries_tags": "en:hungary"}, False, False)
    for b in BRANDS:
        yield (f"brand:{b}", {"brands_tags": b}, True, b in PROTEIN_ONLY_BRANDS)


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
    queries = {q[0]: q for q in all_queries()}
    if resume is None:
        plan = [(label, 1) for label in queries]
    else:
        plan = resume
    try:
        for label, start in plan:
            if label not in queries:
                raise SystemExit(f"unknown query label: {label}")
            _, params, from_brand, protein_only = queries[label]
            run_query(fetcher, params, label, from_brand, protein_only, kept, start)
    except RuntimeError as e:
        print(f"Stopped: {e}", flush=True)
        fetcher.failures.append(str(e))

    out = {
        "source": {
            "provider": "Open Food Facts",
            "license": "ODbL 1.0 (database), DbCL 1.0 (contents)",
            "citation": "© Open Food Facts contributors, https://world.openfoodfacts.org",
            "retrievedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "api": "v2 search",
        },
        "products": [kept[c] for c in sorted(kept)],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"Done: {fetcher.requests} requests, {len(kept)} products -> {OUT_PATH}")
    if fetcher.failures:
        print("Failures:")
        for x in fetcher.failures:
            print("  " + x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
