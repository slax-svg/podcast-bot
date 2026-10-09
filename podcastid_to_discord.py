"""
Korvpallipodcastid -> Discord webhook poster (üks skript, üks webhook)
---------------------------------------------------------------------------
Jälgib kuut korvpalli(ga seotud) podcasti ja postitab uued episoodid
Discordi, kõik läbi ÜHE webhooki. Iga allikas eristub Discordis oma
"username" JA "avatar_url" (oma logo) kaudu.

Postitusse läheb ainult PEALKIRI (+ link) - kirjeldus/body text on
teadlikult eemaldatud, et Discordi kaart oleks lühike ja selge.

Allikad:
  1. Unibet Stuudio   - anchor.fm RSS, filtreeritud "#Korvpall" sildi järgi
  2. Mängumehed       - anchor.fm RSS, kõik episoodid
  3. Pall ei valeta   - Delfi RSS, kõik episoodid
  4. Pihtas-põhjas    - Delfi avalik kategoorialeht (HTML), pealkiri + link
  5. Viies veerandaeg - anchor.fm RSS, episoodid numbriga >= 255
  6. Saku Liigad      - Buzzsprout RSS, kõik episoodid

Logod (avatar_url) on laetud repo "slax-svg/podcast-bot" kausta "logos/"
ja neile viidatakse raw.githubusercontent.com kaudu.

Esimesel käivitusel (kui seen_ids faili pole) postitatakse ainult kõige
uuem episood, ülejäänud märgitakse vaikselt "nähtuks".

Iga allikas on eraldi try/except sees, et üks viga ei takistaks teisi.

Setup
-----
1. pip install feedparser requests beautifulsoup4
2. Loo Discord webhook ja sea env muutuja DISCORD_WEBHOOK_URL_PODCASTS
   (või kleebi URL allpool WEBHOOK_URL kohale).
3. Käivita: python podcastid_to_discord.py
"""

import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# ÜLDINE CONFIG
# ----------------------------------------------------------------------
WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL_PODCASTS",
    "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE",
)

LOGO_BASE = "https://raw.githubusercontent.com/slax-svg/podcast-bot/main/logos/"

LOGO_URLS = {
    "Unibet Stuudio": LOGO_BASE + "unibet.jpg",
    "Mängumehed": LOGO_BASE + "mangumehed.png",
    "Pall ei valeta": LOGO_BASE + "palleivaleta.jpg",
    "Pihtas-põhjas": LOGO_BASE + "pihtas.png",
    "Viies veerandaeg": LOGO_BASE + "viiesveerandaeg.jpg",
    "Saku Liigad": LOGO_BASE + "sakuliigad.jpg",
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, "
        "application/xml, text/html, */*"
    ),
    "Accept-Language": "et-EE,et;q=0.9,en-US;q=0.8,en;q=0.7",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 15

# Maksimaalselt mitu episoodi postitatakse ühe allika kohta ühe käivituse
# jooksul. Ülejäänud märgitakse vaikselt "nähtuks".
MAX_NEW_PER_RUN = 1

# Postitatakse ainult episoode, mis on avaldatud viimase N tunni jooksul.
# Kui kuupäeva ei õnnestu kindlaks teha, postitatakse nagu varem.
MAX_AGE_HOURS = 48

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

HTML_TAG_RE = re.compile(r"<[^>]+>")
DELFI_ARTICLE_LINK_RE = re.compile(r"/(?:artikkel|video)/\d{6,}")


# ----------------------------------------------------------------------
# ÜLDISED ABIFUNKTSIOONID
# ----------------------------------------------------------------------
def normalize_url(url):
    """Eemaldab URL-ilt päringuparameetrid ja fragmendi (nt Delfi
    '?dsrc=...' jälgimisparameeter)."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")


def load_seen(seen_file):
    """Tagastab (seen_set, is_first_run)."""
    if not os.path.exists(seen_file):
        return set(), True
    try:
        with open(seen_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return set(), True
        return set(json.loads(content)), False
    except (json.JSONDecodeError, ValueError) as e:
        name = os.path.basename(seen_file)
        print(f"  ! {name} oli vigane ({e}); alustan tühjalt.")
        return set(), True


def save_seen(seen_file, seen):
    with open(seen_file, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)


def strip_timezone(date_str):
    if not date_str:
        return None
    return re.sub(r"\s*[+-]\d{4}\s*$", "", date_str).strip()


def clean_description(raw):
    if not raw:
        return ""
    return HTML_TAG_RE.sub("", raw).strip()


def fetch_url(url, accept_header=None):
    """Laeb URL'i alla, korduskatsetega. Tagastab (content_bytes, status)
    või viskab RuntimeError'i, kui kõik katsed ebaõnnestusid."""
    headers = dict(REQUEST_HEADERS)
    if accept_header:
        headers["Accept"] = accept_header

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.content, resp.status_code
            print(f"    ! Katse {attempt}/{MAX_RETRIES}: HTTP {resp.status_code}")
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            print(f"    ! Katse {attempt}/{MAX_RETRIES}: viga ({e})")
            last_error = str(e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Kõik {MAX_RETRIES} katset ebaõnnestusid. Viimane viga: {last_error}"
    )


def classify_new(items, seen, is_first_run):
    """Tavajuhul: tagastab kõik, mille id pole veel seen'is.
    Esimesel käivitusel: märgib KÕIK nähtuks, aga tagastab postitamiseks
    ainult kõige esimese (=uusima) elemendi."""
    if is_first_run:
        for item in items:
            seen.add(item["id"])
        return items[:1]

    new_ones = [item for item in items if item["id"] not in seen]
    for item in new_ones:
        seen.add(item["id"])

    if len(new_ones) > MAX_NEW_PER_RUN:
        print(
            f"    ! {len(new_ones)} uut korraga - postitan ainult "
            f"{MAX_NEW_PER_RUN} uusimat, ülejäänud märgitud nähtuks."
        )
        return new_ones[:MAX_NEW_PER_RUN]
    return new_ones


def post_to_discord(item, username, color):
    embed = {
        "title": item["title"],
        "url": item.get("link", ""),
        "color": color,
    }
    footer_text = username
    if item.get("date"):
        footer_text += f" | {item['date']}"
    embed["footer"] = {"text": footer_text}

    payload = {"username": username, "embeds": [embed]}
    logo_url = LOGO_URLS.get(username, "")
    if logo_url and "PASTE_" not in logo_url:
        payload["avatar_url"] = logo_url

    r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if r.status_code >= 300:
        print(f"    ! Discordi postitus ebaõnnestus ({r.status_code}): {r.text[:200]}")
    else:
        print(f"    -> postitatud: {item['title']}")


# ----------------------------------------------------------------------
# RSS/ATOM-PÕHISED ALLIKAD
# ----------------------------------------------------------------------
RSS_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, */*"


def fetch_rss_items(feed_url, filter_func=None, debug=True):
    content, status = fetch_url(feed_url, accept_header=RSS_ACCEPT)
    feed = feedparser.parse(content)
    if debug:
        bozo = getattr(feed, "bozo", "unknown")
        print(f"    Feed status: {status}, bozo: {bozo}, kirjeid: {len(feed.entries)}")

    items = []
    for entry in feed.entries:
        title = (entry.get("title", "") or "").strip()
        raw_summary = entry.get("summary", "") or entry.get("description", "")
        summary = clean_description(raw_summary)

        if filter_func and not filter_func(title, summary):
            continue

        item_id = entry.get("id") or entry.get("link")
        if not item_id:
            continue

        dt = None
        if entry.get("published_parsed"):
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        items.append({
            "id": item_id,
            "title": title,
            "link": entry.get("link", ""),
            "date": strip_timezone(entry.get("published", "")),
            "dt": dt,
        })
    return items


def unibet_korvpall_filter(title, description):
    """Postitame ainult episoodid, mille kirjelduses on hashtag '#Korvpall'."""
    return "#korvpall" in description.lower()


EPISODE_NUM_RE = re.compile(r"^\s*(\d+)\.\s*osa", re.IGNORECASE)
VIIES_VEERANDAEG_MIN_EPISODE = 255


def viies_veerandaeg_filter(title, description):
    """Lubame ainult episoodid numbriga >= VIIES_VEERANDAEG_MIN_EPISODE
    (pealkiri: '284. osa: ...')."""
    m = EPISODE_NUM_RE.match(title)
    return bool(m) and int(m.group(1)) >= VIIES_VEERANDAEG_MIN_EPISODE


# ----------------------------------------------------------------------
# HTML-PÕHINE ALLIKAS (Pihtas-põhjas - avalik Delfi kategoorialeht)
# ----------------------------------------------------------------------
def fetch_pihtaspohjas_items(page_url, debug=True):
    content, status = fetch_url(
        page_url, accept_header="text/html,application/xhtml+xml,*/*"
    )
    soup = BeautifulSoup(content, "html.parser")

    seen_links = set()
    items = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not DELFI_ARTICLE_LINK_RE.search(href):
            continue
        if href.startswith("/"):
            href = "https://sport.delfi.ee" + href
        href = normalize_url(href)

        # Ainult sport.delfi.ee artiklid.
        if not href.startswith("https://sport.delfi.ee/"):
            continue

        title = a_tag.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        if href in seen_links:
            continue
        seen_links.add(href)
        items.append({
            "id": href,
            "title": title,
            "link": href,
            "date": None,
            "dt": None,
        })

    if debug:
        print(f"    HTML status: {status}, leitud artikleid: {len(items)}")
    return items


def _parse_iso(raw):
    """ISO-kuupäev -> ajavööndiga datetime. None, kui ei parsi."""
    if not raw:
        return None
    raw = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_iso_date(raw):
    """'2026-09-23T14:30:00+03:00' -> 'Wed, 23 Sep 2026 14:30:00'."""
    if not raw:
        return None
    raw = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.strftime("%a, %d %b %Y %H:%M:%S")


def fetch_article_date(url):
    """Loeb Delfi artikli enda lehelt avaldamise kuupäeva. Tagastab
    (kuvatav_tekst, datetime) või (None, None)."""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    ! Kuupäeva laadimine ebaõnnestus ({resp.status_code}): {url}")
            return None, None
        soup = BeautifulSoup(resp.content, "html.parser")
    except requests.RequestException as e:
        print(f"    ! Kuupäeva laadimine ebaõnnestus ({e}): {url}")
        return None, None

    candidates = []
    for attrs in (
        {"property": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "article:published_time"},
        {"name": "pubdate"},
        {"property": "og:article:published_time"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(tag["content"])

    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        candidates.append(time_tag["datetime"])

    for script in soup.find_all("script", type="application/ld+json"):
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
        if m:
            candidates.append(m.group(1))

    for raw in candidates:
        formatted = _format_iso_date(raw)
        dt = _parse_iso(raw)
        if formatted and dt:
            return formatted, dt

    print(f"    ! Kuupäeva ei leitud artiklilt: {url}")
    return None, None


# ----------------------------------------------------------------------
# ALLIKATE MÄÄRATLUS
# ----------------------------------------------------------------------
PALL_EI_VALETA_RSS = (
    "https://media-api-podcast-worker.api.delfi.ee/"
    "media-api-podcast-worker/v1/rss-feed/"
    "e6b5f510-1866-430b-a945-5b4d785728c7.rss"
)

SOURCES = [
    {
        "name": "Unibet Stuudio",
        "type": "rss",
        "url": "https://anchor.fm/s/10ec2dabc/podcast/rss",
        "filter": unibet_korvpall_filter,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_unibet.json"),
        "color": 0x5865F2,
    },
    {
        "name": "Mängumehed",
        "type": "rss",
        "url": "https://anchor.fm/s/10f87e35c/podcast/rss",
        "filter": None,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_mangumehed.json"),
        "color": 0x2ECC71,
    },
    {
        "name": "Pall ei valeta",
        "type": "rss",
        "url": PALL_EI_VALETA_RSS,
        "filter": None,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_pallteivaleta.json"),
        "color": 0xE84118,
    },
    {
        "name": "Pihtas-põhjas",
        "type": "html",
        "url": "https://sport.delfi.ee/kategooria/120000743/pihtas-pohjas",
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_pihtaspohjas.json"),
        "color": 0x9B59B6,
    },
    {
        "name": "Viies veerandaeg",
        "type": "rss",
        "url": "https://anchor.fm/s/fdf19c28/podcast/rss",
        "filter": viies_veerandaeg_filter,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_viiesveerandaeg.json"),
        "color": 0xF39C12,
    },
    {
        "name": "Saku Liigad",
        "type": "rss",
        "url": "https://rss.buzzsprout.com/2656012.rss",
        "filter": None,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_sakuliigad.json"),
        "color": 0x1ABC9C,
    },
]


# ----------------------------------------------------------------------
# PÕHIPROTSESS
# ----------------------------------------------------------------------
def process_source(source):
    name = source["name"]
    print(f"\n=== {name} ===")

    if source["type"] == "rss":
        items = fetch_rss_items(source["url"], filter_func=source.get("filter"))
    elif source["type"] == "html":
        items = fetch_pihtaspohjas_items(source["url"])
    else:
        raise ValueError(f"Tundmatu allika tüüp: {source['type']}")

    seen, is_first_run = load_seen(source["seen_file"])

    if source["type"] == "html":
        # Taandame kõik vanad kirjed normaalkujule, et võrdlus klapiks.
        seen = {normalize_url(u) for u in seen}

    if is_first_run:
        print(f"    * Esimene käivitus '{name}' jaoks.")
        print("      Postitan ainult kõige uuema episoodi.")

    new_ones = classify_new(items, seen, is_first_run)
    print(f"    Uusi postitatavaid: {len(new_ones)}")

    # HTML-allika kategoorialehel kuupäeva pole - loeme selle uute artiklite
    # enda lehelt (ainult postitatavate kohta).
    if source["type"] == "html":
        for item in new_ones:
            if not item.get("date"):
                item["date"], item["dt"] = fetch_article_date(item["link"])

    # Ainult värsked episoodid: vanemad märgitud juba nähtuks.
    now = datetime.now(timezone.utc)
    fresh = []
    for item in new_ones:
        dt = item.get("dt")
        if dt and now - dt > timedelta(hours=MAX_AGE_HOURS):
            print(f"    - jätan postitamata (vanem kui {MAX_AGE_HOURS} h): {item['title']}")
            continue
        fresh.append(item)
    new_ones = fresh

    for item in reversed(new_ones):  # vanim enne
        post_to_discord(item, username=name, color=source["color"])
        time.sleep(1)

    save_seen(source["seen_file"], seen)


def main():
    if "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE" in WEBHOOK_URL:
        raise SystemExit(
            "Sea WEBHOOK_URL (või DISCORD_WEBHOOK_URL_PODCASTS env muutuja) "
            "enne käivitamist!"
        )

    for source in SOURCES:
        try:
            process_source(source)
        except Exception as e:
            print(f"  !!! Viga allika '{source['name']}' töötlemisel, jätan vahele: {e}")

    print("\nKõik allikad läbi töödeldud.")


if __name__ == "__main__":
    main()
