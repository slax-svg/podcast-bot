"""
Korvpallipodcastid -> Discord webhook poster (üks skript, üks webhook)
---------------------------------------------------------------------------
Jälgib viit erinevat korvpalli(ga seotud) podcasti/kanalit ja postitab
uued episoodid Discordi, kõik läbi ÜHE webhooki. Iga allikas eristub
Discordis oma "username" ja embed-värvi kaudu.

Allikad:
  1. Unibet Stuudio   - anchor.fm RSS, filtreeritud "#Korvpall" sildi järgi
                        (saates on ka jalgpalli episoode - "Jalkasaade" -
                        mida me EI taha, seega ei kasuta enam pealkirja
                        kontrolli, vaid otsime kirjeldusest hashtagi
                        "#Korvpall", mille Unibet Stuudio ise lisab igale
                        korvpalli-episoodile).
  2. Mängumehed       - anchor.fm RSS, puhtalt korvpallipodcast, kõik
                        episoodid postitatakse.
  3. Pall ei valeta   - Delfi enda RSS-i-taolinen API, puhtalt
                        korvpallipodcast, kõik episoodid postitatakse.
  4. Kuues Viga (NBA) - Spotify'l avalikku RSS'i ei ole (Spotify-only
                        hostimine), seega kasutame YouTube-kanali enda
                        avalikku video-RSS'i (ei vaja autentimist).
  5. Pihtas-põhjas    - Delfil pole avalikku RSS'i (audio on Tasku
                        tellimuse taga), seega loeme nende AVALIKKU
                        kategoorialehte (HTML), mis näitab iga uue
                        episoodi anonss-artiklit. Postitame ainult
                        pealkirja + lingi, mitte audiot.

Miks "esimesel käivitusel ainult viimane uus episood"
----------------------------------------------------------
Kui mõne allika jaoks pole veel seen_ids faili (uus allikas), siis
esimesel käivitusel EI postitata kogu olemasolevat episoodide ajalugu -
ainult kõige uuem episood postitatakse, ülejäänud märgitakse vaikselt
"juba nähtuks". Nii ei upu Discordi kanal esimesel käivitusel
kümnete vanade episoodidega.

Miks iga allikas on eraldi try/except sees
-----------------------------------------------
Kui üks allikas ebaõnnestub (nt Delfi muudab oma lehe struktuuri, või
mõni server ajutiselt ei vasta), ei tohi see takistada teiste allikate
postitamist. Viga logitakse selgelt, aga skript jätkab järgmise
allikaga.

Miks kõiki kontrollitakse iga 6h tagant
--------------------------------------------
Vt kaasasolevat .yml faili - üks ühtne ajakava kõigile viiele allikale,
lihtsuse huvides. Pihtas-põhjas ilmub tavaliselt pühapäeva õhtul kell
~21:46, seega 6h kontrollisagedusega võib see avastatuks saada kuni
mõni tund hiljem - see on aktsepteeritav, kuna tegu pole kiireloomulise
sisuga.

Setup
-----
1. pip install feedparser requests beautifulsoup4
2. Loo Discord webhook (üks, jagatud kõigi allikate vahel):
   Kanal -> Redigeeri kanalit -> Integratsioonid -> Webhookid -> Uus -> Kopeeri URL
3. Kleebi see URL alla WEBHOOK_URL kohale (või sea DISCORD_WEBHOOK_URL_PODCASTS env muutuja).
4. Käivita: python podcastid_to_discord.py
   Või kasuta GitHub Actionsit (vt kaasasolevat .yml faili).
"""

import os
import re
import json
import time
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

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html, */*",
    "Accept-Language": "et-EE,et;q=0.9,en-US;q=0.8,en;q=0.7",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 15
DESCRIPTION_MAX_LENGTH = 300

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_TAG_RE = re.compile(r"<[^>]+>")
DELFI_ARTICLE_LINK_RE = re.compile(r"/(?:artikkel|video)/\d{6,}")


# ----------------------------------------------------------------------
# ÜLDISED ABIFUNKTSIOONID
# ----------------------------------------------------------------------
def load_seen(seen_file):
    """Tagastab (seen_set, is_first_run). is_first_run=True, kui faili
    veel polnud olemas või see oli tühi - sel juhul postitame edaspidi
    ainult kõige uuema episoodi, mitte kogu ajalugu."""
    if not os.path.exists(seen_file):
        return set(), True
    try:
        with open(seen_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return set(), True
        return set(json.loads(content)), False
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ! {os.path.basename(seen_file)} oli vigane ({e}); alustan tühjalt.")
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


def truncate_description(text, max_length=DESCRIPTION_MAX_LENGTH):
    if not text or len(text) <= max_length:
        return text
    cut = text[:max_length].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{cut}..."


def fetch_url(url, accept_header=None):
    """Laeb URL'i alla, korduskatsetega. Tagastab (content_bytes, status)
    või (None, None), kui kõik katsed ebaõnnestusid."""
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

    raise RuntimeError(f"Kõik {MAX_RETRIES} katset ebaõnnestusid. Viimane viga: {last_error}")


def classify_new(items, seen, is_first_run):
    """Tavajuhul: tagastab kõik, mille id pole veel seen'is.
    Esimesel käivitusel: märgib KÕIK nähtuks, aga tagastab postitamiseks
    ainult kõige esimese (=uusima, eeldusel et feed on uusim-enne
    järjestuses) elemendi."""
    if is_first_run:
        for item in items:
            seen.add(item["id"])
        return items[:1]

    new_ones = [item for item in items if item["id"] not in seen]
    for item in new_ones:
        seen.add(item["id"])
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
    r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if r.status_code >= 300:
        print(f"    ! Discordi postitus ebaõnnestus ({r.status_code}): {r.text[:200]}")
    else:
        print(f"    -> postitatud: {item['title']}")


# ----------------------------------------------------------------------
# RSS/ATOM-PÕHISED ALLIKAD (Unibet Stuudio, Mängumehed, Pall ei valeta, Kuues Viga)
# ----------------------------------------------------------------------
def fetch_rss_items(feed_url, filter_func=None, debug=True):
    content, status = fetch_url(feed_url, accept_header="application/rss+xml, application/atom+xml, application/xml, */*")
    feed = feedparser.parse(content)

    if debug:
        print(f"    Feed status: {status}, bozo: {getattr(feed, 'bozo', 'unknown')}, kirjeid: {len(feed.entries)}")

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

        items.append({
            "id": item_id,
            "title": title,
            "link": entry.get("link", ""),
            "description": truncate_description(summary) if summary else "",
            "date": strip_timezone(entry.get("published", "")),
        })
    return items


def unibet_korvpall_filter(title, description):
    """Postitame ainult episoodid, mille kirjelduses on hashtag
    '#Korvpall' - see eristab korvpalli-episoodid (Kossusaade) saates
    olevatest jalgpalli-episoodidest (Jalkasaade)."""
    return "#korvpall" in description.lower()


# ----------------------------------------------------------------------
# HTML-PÕHINE ALLIKAS (Pihtas-põhjas - avalik Delfi kategoorialeht)
# ----------------------------------------------------------------------
def fetch_pihtaspohjas_items(page_url, debug=True):
    content, status = fetch_url(page_url, accept_header="text/html,application/xhtml+xml,*/*")
    soup = BeautifulSoup(content, "html.parser")

    seen_links = set()
    items = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not DELFI_ARTICLE_LINK_RE.search(href):
            continue
        if href.startswith("/"):
            href = "https://sport.delfi.ee" + href

        title = a_tag.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        if href in seen_links:
            continue
        seen_links.add(href)

        items.append({"id": href, "title": title, "link": href, "description": "", "date": None})

    if debug:
        print(f"    HTML status: {status}, leitud artikleid: {len(items)}")

    return items


# ----------------------------------------------------------------------
# ALLIKATE MÄÄRATLUS
# ----------------------------------------------------------------------
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
        "url": "https://media-api-podcast-worker.api.delfi.ee/media-api-podcast-worker/v1/rss-feed/e6b5f510-1866-430b-a945-5b4d785728c7.rss",
        "filter": None,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_pallteivaleta.json"),
        "color": 0xE84118,
    },
    {
        "name": "Kuues Viga",
        "type": "rss",
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC32OZ0maQSJzsGD57TgDkgg",
        "filter": None,
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_kuuesviga.json"),
        "color": 0xFF0000,
    },
    {
        "name": "Pihtas-põhjas",
        "type": "html",
        "url": "https://sport.delfi.ee/kategooria/120000743/pihtas-pohjas",
        "seen_file": os.path.join(SCRIPT_DIR, "seen_ids_pihtaspohjas.json"),
        "color": 0x9B59B6,
    },
]


# ----------------------------------------------------------------------
# PÕHIPROTSESS
# ----------------------------------------------------------------------
def process_source(source):
    print(f"\n=== {source['name']} ===")

    if source["type"] == "rss":
        items = fetch_rss_items(source["url"], filter_func=source.get("filter"))
    elif source["type"] == "html":
        items = fetch_pihtaspohjas_items(source["url"])
    else:
        raise ValueError(f"Tundmatu allika tüüp: {source['type']}")

    seen, is_first_run = load_seen(source["seen_file"])
    if is_first_run:
        print(f"    * Esimene käivitus '{source['name']}' jaoks - postitan ainult kõige uuema episoodi.")

    new_ones = classify_new(items, seen, is_first_run)
    print(f"    Uusi postitatavaid: {len(new_ones)}")

    for item in reversed(new_ones):  # vanim enne
        post_to_discord(item, username=source["name"], color=source["color"])
        time.sleep(1)

    save_seen(source["seen_file"], seen)


def main():
    if "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE" in WEBHOOK_URL:
        raise SystemExit("Sea WEBHOOK_URL (või DISCORD_WEBHOOK_URL_PODCASTS env muutuja) enne käivitamist!")

    for source in SOURCES:
        try:
            process_source(source)
        except Exception as e:
            print(f"  !!! Viga allika '{source['name']}' töötlemisel, jätan vahele: {e}")

    print("\nKõik allikad läbi töödeldud.")


if __name__ == "__main__":
    main()
