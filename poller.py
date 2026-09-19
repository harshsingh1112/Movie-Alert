#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}


def load_json(path, default=None):
    if not path.exists():
        return default

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_config():
    cfg = load_json(CONFIG_PATH, default={}) or {}

    env_map = {
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }

    for env_key, cfg_key in env_map.items():
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]

    required = [
        "movie",
        "movie_code",
        "city",
        "base_url",
        "telegram_bot_token",
        "telegram_chat_id",
    ]

    missing = [key for key in required if not cfg.get(key)]

    if missing:
        sys.exit(
            "Missing required config: " + ", ".join(missing)
        )

    return cfg


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )

    response.raise_for_status()


def fetch_url(cfg, url):
    headers = dict(DEFAULT_HEADERS)

    scraper_key = os.environ.get("SCRAPERAPI_KEY")

    if scraper_key:
        api_url = (
            "https://api.scraperapi.com/?"
            + urllib.parse.urlencode(
                {
                    "api_key": scraper_key,
                    "country_code": "in",
                    "url": url,
                }
            )
        )

        response = requests.get(
            api_url,
            timeout=90,
        )

        response.raise_for_status()
        return response.text

    response = requests.get(
        url,
        headers=headers,
        timeout=45,
    )

    response.raise_for_status()
    return response.text


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def find_dates(page_text):
    """
    Find YYYYMMDD date tokens exposed by BookMyShow.
    """

    tokens = re.findall(r"\b20\d{6}\b", page_text)

    dates = set()

    today = date.today()

    for token in tokens:
        try:
            parsed = datetime.strptime(token, "%Y%m%d").date()

            # Ignore obviously old dates.
            if parsed >= today:
                dates.add(token)

        except ValueError:
            continue

    return sorted(dates)


def date_url(cfg, requested_date):
    """
    Build the BookMyShow date page.
    """

    return (
        f"{cfg['base_url']}/buytickets/"
        f"{cfg['movie_code']}/"
        f"{requested_date}"
        f"?etCodes={cfg['movie_code']}"
        f"&language=english"
    )


def clean_show_url(url):
    if not url:
        return ""

    if url.startswith("/"):
        return "https://in.bookmyshow.com" + url

    return url


def extract_show_links(html, requested_date, cfg):
    """
    Extract BookMyShow seat-layout/show identifiers.

    Example:

    /seat-layout/ET00516728/ALUC/4977/20260925
    """

    pattern = re.compile(
        rf"/seat-layout/"
        rf"{re.escape(cfg['movie_code'])}"
        rf"/([^/?\"' ]+)"
        rf"/([^/?\"' ]+)"
        rf"/{requested_date}"
    )

    matches = pattern.findall(html)

    shows = []

    seen = set()

    for venue_code, show_code in matches:

        key = (
            requested_date,
            venue_code,
            show_code,
        )

        if key in seen:
            continue

        seen.add(key)

        booking_url = (
            "https://in.bookmyshow.com"
            f"/movies/{cfg['city']}/"
            f"{cfg['movie_slug']}/"
            f"buytickets/{cfg['movie_code']}/"
            f"{requested_date}"
            f"?etCodes={cfg['movie_code']}"
            f"&language=english"
        )

        seat_url = (
            "https://in.bookmyshow.com"
            f"/seat-layout/{cfg['movie_code']}/"
            f"{venue_code}/{show_code}/{requested_date}"
        )

        shows.append(
            {
                "date": requested_date,
                "venue_code": venue_code,
                "show_code": show_code,
                "booking_url": booking_url,
                "seat_url": seat_url,
            }
        )

    return shows


def find_theatre_name(html, venue_code):
    """
    Try to recover the theatre name from the HTML around the
    venue code.

    BookMyShow changes its markup periodically, so this is
    intentionally heuristic.
    """

    soup = BeautifulSoup(html, "html.parser")

    # Look for elements containing the venue code.
    matches = soup.find_all(
        string=lambda text: text and venue_code.lower() in text.lower()
    )

    for match in matches:
        parent = match.parent

        if not parent:
            continue

        text = normalize(parent.get_text(" ", strip=True))

        if 3 <= len(text) <= 180:
            return text

        # Try a few ancestors.
        ancestor = parent

        for _ in range(5):
            ancestor = ancestor.parent

            if not ancestor:
                break

            text = normalize(
                ancestor.get_text(" ", strip=True)
            )

            if 3 <= len(text) <= 180:
                return text

    return f"Venue {venue_code}"


def extract_showtimes(html):
    """
    Extract visible time strings.

    This is deliberately conservative. It is used to enrich
    notifications; the unique show identity is still based
    on the BMS show identifier.
    """

    pattern = re.compile(
        r"\b(?:0?[1-9]|1[0-2]):[0-5]\d\s?(?:AM|PM)\b",
        re.IGNORECASE,
    )

    return sorted(
        set(
            normalize(match.upper())
            for match in pattern.findall(
                BeautifulSoup(
                    html,
                    "html.parser",
                ).get_text(" ", strip=True)
            )
        )
    )


def format_is_present(html, cfg):
    """
    Confirm that the requested language/format is represented
    on the page.

    The exact BMS label currently used is:
    MS-Infinity Vsn 3d
    """

    text = normalize(
        BeautifulSoup(
            html,
            "html.parser",
        ).get_text(" ", strip=True)
    ).lower()

    required_format = cfg.get(
        "format",
        "ms-infinity vsn 3d",
    ).lower()

    format_variants = [
        required_format,
        "ms-infinity vsn 3d",
        "ms infinity vision 3d",
        "ms-infinity vision 3d",
    ]

    has_format = any(
        variant in text
        for variant in format_variants
    )

    has_english = "english" in text

    return has_format and has_english


def discover_shows(cfg, landing_html):
    """
    Discover every currently exposed date, then inspect each
    date-specific BookMyShow page.

    New dates are therefore discovered automatically when
    BookMyShow adds them to the movie page.
    """

    dates = find_dates(landing_html)

    print(f"Discovered dates: {dates}")

    all_shows = []

    for requested_date in dates:

        print(
            f"Checking BookMyShow date {requested_date}..."
        )

        url = date_url(
            cfg,
            requested_date,
        )

        try:
            html = fetch_url(
                cfg,
                url,
            )

        except requests.RequestException as exc:
            print(
                f"Failed to fetch {requested_date}: {exc}"
            )
            continue

        if not format_is_present(
            html,
            cfg,
        ):
            print(
                f"Requested format not detected for "
                f"{requested_date}"
            )
            continue

        shows = extract_show_links(
            html,
            requested_date,
            cfg,
        )

        showtimes = extract_showtimes(html)

        for show in shows:

            show["showtimes_seen"] = showtimes
            show["theatre"] = find_theatre_name(
                html,
                show["venue_code"],
            )

            all_shows.append(show)

        # Be polite to the site/proxy.
        time.sleep(1)

    return all_shows


def make_show_key(show):
    return "|".join(
        [
            show.get("date", ""),
            show.get("venue_code", ""),
            show.get("show_code", ""),
        ]
    )


def pretty_date(value):
    try:
        return datetime.strptime(
            value,
            "%Y%m%d",
        ).strftime("%d %b %Y")

    except ValueError:
        return value


def notify_new_shows(cfg, new_shows):
    """
    Send one Telegram message containing all newly detected
    shows from this scan.
    """

    if not new_shows:
        return

    lines = [
        "🎬 Avengers Endgame: Encore",
        "🆕 New BookMyShow show detected!",
        "",
        "Language: English",
        "Format: MS-Infinity Vision 3D",
        "",
    ]

    for show in new_shows:

        lines.extend(
            [
                f"📅 {pretty_date(show['date'])}",
                f"🏢 {show['theatre']}",
                f"🎟️ Show: {show['show_code']}",
                f"🔗 {show['seat_url']}",
                "",
            ]
        )

    send_telegram(
        cfg["telegram_bot_token"],
        cfg["telegram_chat_id"],
        "\n".join(lines),
    )


def main():

    cfg = load_config()

    state = load_json(
        STATE_PATH,
        default={
            "shows": {}
        },
    ) or {
        "shows": {}
    }

    print(
        f"Checking {cfg['movie']} "
        f"in {cfg['city']}..."
    )

    try:
        landing_html = fetch_url(
            cfg,
            cfg["base_url"],
        )

    except requests.RequestException as exc:
        print(
            f"Landing page fetch failed: {exc}"
        )
        return 0

    current_shows = discover_shows(
        cfg,
        landing_html,
    )

    print(
        f"Detected {len(current_shows)} show records."
    )

    current_state = {}

    for show in current_shows:

        key = make_show_key(show)

        current_state[key] = show

    previous_state = state.get(
        "shows",
        {},
    )

    new_keys = [
        key
        for key in current_state
        if key not in previous_state
    ]

    new_shows = [
        current_state[key]
        for key in new_keys
    ]

    print(
        f"New shows detected: {len(new_shows)}"
    )

    if new_shows:

        notify_new_shows(
            cfg,
            new_shows,
        )

        print(
            "Telegram notification sent."
        )

    save_json(
        STATE_PATH,
        {
            "shows": current_state,
            "checked_at": int(time.time()),
        },
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
