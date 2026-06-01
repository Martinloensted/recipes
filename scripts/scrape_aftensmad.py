#!/usr/bin/env python3
"""Scrape Aftensmad recipes from vegetariskhverdag.dk and merge into recipes.json."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://vegetariskhverdag.dk"
CATEGORY_URL = f"{BASE_URL}/opskrifter/"
RECIPES_FILE = Path(__file__).parent.parent / "recipes.json"
DELAY = 0.7  # seconds between requests
TARGET = 150
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; recipe-scraper/1.0; +https://github.com/)"
})


def get_soup(url: str) -> BeautifulSoup:
    time.sleep(DELAY)
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[-1] if parts else url


def discover_aftensmad_urls() -> list[str]:
    """Return deduplicated list of Aftensmad recipe page URLs."""
    seen: set[str] = set()
    urls: list[str] = []

    page = 1
    while len(urls) < TARGET:
        listing = f"{CATEGORY_URL}?kategori=aftensmad" + (f"&side={page}" if page > 1 else "")
        print(f"  Listing page {page}: {listing}")
        try:
            soup = get_soup(listing)
        except Exception as e:
            print(f"    Error: {e}")
            break

        cards = soup.select("a[href*='/opskrift/'], a[href*='/opskrifter/'][href$='/']")
        # Broaden: any link that looks like a recipe slug
        if not cards:
            cards = soup.select(".recipe-card a, .opskrift-card a, article a, .card a")

        found_on_page = 0
        for a in cards:
            href = a.get("href", "")
            if not href:
                continue
            full = urljoin(BASE_URL, href)
            # Skip category/listing pages
            if full.rstrip("/") in (CATEGORY_URL.rstrip("/"), BASE_URL.rstrip("/")):
                continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
                found_on_page += 1

        print(f"    Found {found_on_page} new URLs (total {len(urls)})")
        if found_on_page == 0:
            break
        page += 1

    return urls[:TARGET]


def parse_time(text: str) -> int:
    """Convert a Danish time string like '45 min', '1 time 15 min' to minutes."""
    if not text:
        return 0
    text = text.lower()
    hours = re.search(r"(\d+)\s*time", text)
    mins = re.search(r"(\d+)\s*min", text)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    return total


def parse_recipe(url: str) -> dict | None:
    """Fetch a recipe page and return a structured dict, or None on failure."""
    try:
        soup = get_soup(url)
    except Exception as e:
        print(f"    Fetch error {url}: {e}")
        return None

    title_el = soup.select_one("h1")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)

    # Prep time — look for common patterns
    prep_time = 0
    for sel in [".prep-time", ".tid", "[class*='time']", "[class*='tid']"]:
        el = soup.select_one(sel)
        if el:
            prep_time = parse_time(el.get_text())
            if prep_time:
                break
    if not prep_time:
        # Search text near "tid" or "min"
        for el in soup.find_all(string=re.compile(r"\d+\s*min", re.I)):
            t = parse_time(str(el))
            if t:
                prep_time = t
                break

    # Servings
    base_servings = 2
    for sel in [".servings", ".portioner", "[class*='serving']", "[class*='portion']"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r"\d+", el.get_text())
            if m:
                base_servings = int(m.group())
                break
    if base_servings == 2:
        for el in soup.find_all(string=re.compile(r"(\d+)\s*(portioner?|personer?)", re.I)):
            m = re.search(r"(\d+)\s*(portioner?|personer?)", str(el), re.I)
            if m:
                base_servings = int(m.group(1))
                break

    # Image URL from og:image
    og_image = soup.select_one('meta[property="og:image"]')
    image_url = og_image["content"].strip() if og_image and og_image.get("content") else ""

    # Tags
    tags = ["Vegetar"]
    for sel in [".tag", ".tags a", ".recipe-tag", "[class*='tag']", ".category a"]:
        for el in soup.select(sel):
            t = el.get_text(strip=True)
            if t and t not in tags:
                tags.append(t)

    # Ingredients
    ingredients: list[dict] = []
    for sel in [
        ".ingredients li",
        ".ingredienser li",
        "[class*='ingredient'] li",
        "[class*='ingrediens'] li",
        "ul.ingredients li",
    ]:
        items = soup.select(sel)
        if items:
            for li in items:
                raw = li.get_text(separator=" ", strip=True)
                if not raw:
                    continue
                # Try to split amount + unit + name
                m = re.match(
                    r"^([\d½¼¾\.,/\-]+)\s*"
                    r"(dl|l|ml|g|kg|spsk|tsk|stk|fed|håndfuld|nip|bunch|bundt|pakke|ds|dåse|pose)?\s*"
                    r"(.+)$",
                    raw,
                    re.I,
                )
                if m:
                    ingredients.append({
                        "amount": m.group(1).strip(),
                        "unit": (m.group(2) or "").strip(),
                        "name": m.group(3).strip(),
                    })
                else:
                    ingredients.append({"amount": "", "unit": "", "name": raw})
            break

    slug = slug_from_url(url)
    return {
        "id": f"vh-{slug}",
        "title": title,
        "category": "Aftensmad",
        "prepTimeMin": prep_time,
        "tags": tags,
        "imageUrl": image_url,
        "ingredients": ingredients,
        "source": "vegetariskhverdag",
        "url": url,
        "baseServings": base_servings,
    }


def load_existing() -> list[dict]:
    if not RECIPES_FILE.exists() or RECIPES_FILE.stat().st_size == 0:
        return []
    try:
        return json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def main() -> None:
    print("=== Vegetarisk Hverdag — Aftensmad scraper ===")

    existing = load_existing()
    existing_ids = {r["id"] for r in existing}
    existing_urls = {r["url"] for r in existing}
    print(f"Existing recipes: {len(existing)}")

    # Backfill imageUrl for any existing recipe that is missing it
    needs_image = [r for r in existing if not r.get("imageUrl")]
    if needs_image:
        print(f"Backfilling imageUrl for {len(needs_image)} existing recipes...")
        for r in needs_image:
            print(f"  {r['url']}")
            try:
                soup = get_soup(r["url"])
                og = soup.select_one('meta[property="og:image"]')
                r["imageUrl"] = og["content"].strip() if og and og.get("content") else ""
                print(f"    -> {r['imageUrl'][:80]}" if r["imageUrl"] else "    -> (not found)")
            except Exception as e:
                print(f"    Error: {e}")

    print("Discovering Aftensmad URLs...")
    all_urls = discover_aftensmad_urls()
    new_urls = [u for u in all_urls if u not in existing_urls]
    print(f"URLs discovered: {len(all_urls)}, new: {len(new_urls)}")

    scraped: list[dict] = []
    for i, url in enumerate(new_urls, 1):
        print(f"  [{i}/{len(new_urls)}] {url}")
        recipe = parse_recipe(url)
        if recipe and recipe["id"] not in existing_ids:
            scraped.append(recipe)
            existing_ids.add(recipe["id"])
        else:
            print("    Skipped (parse failed or duplicate)")

    print(f"Scraped {len(scraped)} new recipes")

    combined = existing + scraped
    # Sort: ≤45 min first, then ascending by time
    combined.sort(key=lambda r: (r.get("prepTimeMin") or 999 > 45, r.get("prepTimeMin") or 999))

    RECIPES_FILE.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(combined)} total recipes to {RECIPES_FILE}")


if __name__ == "__main__":
    main()
