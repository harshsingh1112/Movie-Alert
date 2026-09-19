#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, date, timedelta
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


# ---------------------------------------------------------
# BASIC FILE / CONFIG HELPERS
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# HTTP
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# DATE DISCOVERY
# ---------------------------------------------------------

def find_dates(page_text):
    """
    Find BookMyShow YYYYMMDD dates currently exposed
    by the movie page.
    """

    tokens = re.findall(r"\b20\d{6}\b", page_text)

    dates = set()
    today = date.today()

    for token in tokens:
        try:
            parsed = datetime.strptime(
                token,
                "%Y%m%d"
            ).date()

            if parsed >= today:
                dates.add(token)

        except ValueError:
            continue

    return sorted(dates)


def fallback_dates():
    """
    If BookMyShow does not expose the date selector in the
    landing page, check a rolling 7-day window.

    This is only a fallback.
    """

    today = date.today()

    return [
        (today + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(8)
    ]


# ---------------------------------------------------------
# URLS
# ---------------------------------------------------------

def date_url(cfg, requested_date):
    return (
        f"https://in.bookmyshow.com/movies/"
        f"{cfg['city']}/"
        f"{cfg['movie_slug']}/buytickets/"
        f"{cfg['movie_code']}/"
        f"{requested_date}"
        f"?etCodes={cfg['movie_code']}"
        f"&language=english"
    )


# ---------------------------------------------------------
# BOOKMYSHOW INITIAL STATE
# ---------------------------------------------------------

def extract_initial_state(html):
    """
    BookMyShow embeds its showtime data inside:

        window.__INITIAL_STATE__

    The uploaded BMS response confirmed this structure.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for script in soup.find_all("script"):

        text = script.string or script.get_text()

        if not text:
            continue

        if "window.__INITIAL_STATE__" not in text:
            continue

        start = text.find("{")

        if start == -1:
            continue

        json_text = text[start:].strip()

        try:
            return json.loads(json_text)

        except json.JSONDecodeError:
            continue

    return None


# ---------------------------------------------------------
# GENERIC JSON WALKER
# ---------------------------------------------------------

def walk_json(value):
    """
    Recursively walk every object inside the BMS JSON.
    """

    if isinstance(value, dict):

        yield value

        for child in value.values():
            yield from walk_json(child)

    elif isinstance(value, list):

        for child in value:
            yield from walk_json(child)


# ---------------------------------------------------------
# FORMAT DETECTION
# ---------------------------------------------------------

def get_format_text(show):
    """
    Extract the exact BMS format label from the show object.
    """

    try:
        widgets = (
            show
            .get("customGestureCTA", {})
            .get("additionalData", {})
            .get("bottomSheetData", {})
            .get("widgets", [])
        )

        for widget in widgets:

            variable_data = widget.get(
                "variableData",
                {}
            )

            value = variable_data.get(
                "format",
                ""
            )

            if value:
                return value

    except Exception:
        pass

    return ""


def is_target_format(format_text, cfg):
    """
    Only accept:

        English • MS-Infinity Vsn 3d

    The screen/technology after the | is allowed to vary.
    """

    text = (format_text or "").lower()

    required_format = cfg.get(
        "format",
        "MS-Infinity Vsn 3d"
    ).lower()

    english_ok = "english" in text

    infinity_ok = (
        required_format in text
        or "ms-infinity vsn 3d" in text
        or "ms infinity vision 3d" in text
        or "ms-infinity vision 3d" in text
    )

    return english_ok and infinity_ok


# ---------------------------------------------------------
# AVAILABILITY
# ---------------------------------------------------------

def get_seat_statuses(show):
    """
    Extract seat availability labels from the show's
    bottom-sheet data.

    Examples seen in BMS:
        SOLD OUT
        ALMOST FULL
    """

    statuses = []

    for item in walk_json(show):

        if not isinstance(item, dict):
            continue

        value = item.get("seatAvalibility")

        if value:
            statuses.append(str(value).strip().upper())

    return sorted(set(statuses))


def is_bookable(show):
    """
    Determine whether the show appears to have at least
    some bookable inventory.

    availStatus=1 means BMS considers the session active.

    If seat-level availability is present, SOLD OUT means
    not currently bookable. Any other seat state is treated
    as potentially bookable.
    """

    additional = show.get(
        "additionalData",
        {}
    )

    if str(
        additional.get("availStatus", "")
    ) != "1":
        return False

    statuses = get_seat_statuses(show)

    if not statuses:
        return True

    return any(
        status != "SOLD OUT"
        for status in statuses
    )


# ---------------------------------------------------------
# SHOW EXTRACTION
# ---------------------------------------------------------

def extract_shows_from_html(
    html,
    requested_date,
    cfg
):
    """
    Extract:

        Date
        Theatre
        Venue code
        Showtime
        Session ID
        Format
        Availability
    """

    state = extract_initial_state(html)

    if not state:
        print(
            f"Could not find BookMyShow INITIAL_STATE "
            f"for {requested_date}"
        )
        return []

    shows = []

    # Find every BMS venue-card in the JSON.
    venue_cards = [
        item
        for item in walk_json(state)
        if isinstance(item, dict)
        and item.get("type") == "venue-card"
    ]

    print(
        f"Found {len(venue_cards)} theatre cards "
        f"for {requested_date}"
    )

    for venue in venue_cards:

        venue_data = venue.get(
            "additionalData",
            {}
        )

        venue_code = venue_data.get(
            "venueCode",
            ""
        )

        theatre = venue_data.get(
            "venueName",
            f"Venue {venue_code}"
        )

        sections = venue.get(
            "showtimesSections",
            []
        )

        for section in sections:

            for show in section.get(
                "showtimes",
                []
            ):

                show_data = show.get(
                    "additionalData",
                    {}
                )

                session_id = str(
                    show_data.get(
                        "sessionId",
                        ""
                    )
                )

                show_date = (
                    show_data.get(
                        "showDateCode"
                    )
                    or requested_date
                )

                show_time = (
                    show_data.get(
                        "showTime"
                    )
                    or show.get(
                        "title",
                        ""
                    )
                )

                format_text = get_format_text(
                    show
                )

                # Ignore everything except English +
                # MS Infinity Vision 3D.
                if not is_target_format(
                    format_text,
                    cfg
                ):
                    continue

                if not session_id:
                    continue

                booking_url = date_url(
                    cfg,
                    show_date
                )

                statuses = get_seat_statuses(
                    show
                )

                record = {
                    "date": show_date,
                    "venue_code": venue_code,
                    "theatre": theatre,
                    "time": show_time,
                    "session_id": session_id,
                    "format": format_text,
                    "attributes": show_data.get(
                        "attributes",
                        show.get(
                            "screenAttr",
                            ""
                        )
                    ),
                    "avail_status": str(
                        show_data.get(
                            "availStatus",
                            ""
                        )
                    ),
                    "seat_statuses": statuses,
                    "bookable": is_bookable(
                        show
                    ),
                    "booking_url": booking_url,
                }

                shows.append(record)

    return shows


# ---------------------------------------------------------
# SHOW KEY
# ---------------------------------------------------------

def make_show_key(show):
    """
    A show is uniquely identified by:

        Date + Theatre + Session ID
    """

    return "|".join(
        [
            show.get("date", ""),
            show.get("venue_code", ""),
            show.get("session_id", ""),
        ]
    )


# ---------------------------------------------------------
# DATE FORMAT
# ---------------------------------------------------------

def pretty_date(value):

    try:
        return datetime.strptime(
            value,
            "%Y%m%d"
        ).strftime("%d %b %Y")

    except ValueError:
        return value


# ---------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------

def discover_shows(cfg, landing_html):

    dates = find_dates(
        landing_html
    )

    if dates:
        print(
            f"Discovered dates from BMS: {dates}"
        )

    else:
        dates = fallback_dates()

        print(
            "BMS did not expose date tokens on "
            f"the landing page. Using fallback dates: {dates}"
        )

    all_shows = []

    for requested_date in dates:

        print(
            f"Checking BookMyShow date "
            f"{requested_date}..."
        )

        url = date_url(
            cfg,
            requested_date
        )

        try:

            html = fetch_url(
                cfg,
                url
            )

        except requests.RequestException as exc:

            print(
                f"Failed to fetch "
                f"{requested_date}: {exc}"
            )

            continue

        shows = extract_shows_from_html(
            html,
            requested_date,
            cfg
        )

        print(
            f"Matching shows on "
            f"{requested_date}: {len(shows)}"
        )

        all_shows.extend(
            shows
        )

        # Small delay between requests.
        time.sleep(1)

    return all_shows


# ---------------------------------------------------------
# TELEGRAM NOTIFICATIONS
# ---------------------------------------------------------

def notify_new_shows(
    cfg,
    new_shows
):

    if not new_shows:
        return

    lines = [
        "🎬 Avengers Endgame: Encore",
        "🆕 NEW MS INFINITY VISION 3D SHOW",
        "",
        "Language: English",
        "Format: MS-Infinity Vision 3D",
        "",
    ]

    for show in new_shows:

        status = (
            "🟢 BOOKABLE"
            if show.get("bookable")
            else "🟡 LISTED"
        )

        lines.extend(
            [
                status,
                f"📅 {pretty_date(show['date'])}",
                f"🏢 {show['theatre']}",
                f"🕐 {show['time']}",
                f"🎟️ Session: {show['session_id']}",
            ]
        )

        if show.get("attributes"):
            lines.append(
                f"🖥️ {show['attributes']}"
            )

        if show.get("seat_statuses"):
            lines.append(
                "💺 " + ", ".join(
                    show["seat_statuses"]
                )
            )

        lines.extend(
            [
                f"🔗 {show['booking_url']}",
                "",
            ]
        )

    send_telegram(
        cfg["telegram_bot_token"],
        cfg["telegram_chat_id"],
        "\n".join(lines)
    )


def notify_became_bookable(
    cfg,
    shows
):

    if not shows:
        return

    lines = [
        "🚨 AVENGERS ENDGAME: ENCORE",
        "🟢 SHOW IS NOW BOOKABLE",
        "",
        "Language: English",
        "Format: MS-Infinity Vision 3D",
        "",
    ]

    for show in shows:

        lines.extend(
            [
                f"📅 {pretty_date(show['date'])}",
                f"🏢 {show['theatre']}",
                f"🕐 {show['time']}",
                f"🎟️ Session: {show['session_id']}",
                f"🔗 {show['booking_url']}",
                "",
            ]
        )

    send_telegram(
        cfg["telegram_bot_token"],
        cfg["telegram_chat_id"],
        "\n".join(lines)
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():

    cfg = load_config()

    state = load_json(
        STATE_PATH,
        default={
            "shows": {}
        }
    ) or {
        "shows": {}
    }

    print(
        f"Checking {cfg['movie']} "
        f"in {cfg['city']}..."
    )

    # -----------------------------------------------------
    # Fetch landing page
    # -----------------------------------------------------

    try:

        landing_html = fetch_url(
            cfg,
            cfg["base_url"]
        )

    except requests.RequestException as exc:

        print(
            f"Landing page fetch failed: {exc}"
        )

        return 0

    # -----------------------------------------------------
    # Discover current shows
    # -----------------------------------------------------

    current_shows = discover_shows(
        cfg,
        landing_html
    )

    print(
        f"Detected "
        f"{len(current_shows)} "
        f"matching show records."
    )

    current_state = {}

    for show in current_shows:

        key = make_show_key(
            show
        )

        current_state[key] = show

    previous_state = state.get(
        "shows",
        {}
    )

    # -----------------------------------------------------
    # NEW SHOWS
    # -----------------------------------------------------

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
        f"New shows detected: "
        f"{len(new_shows)}"
    )

    if new_shows:

        notify_new_shows(
            cfg,
            new_shows
        )

        print(
            "New-show Telegram notification sent."
        )

    # -----------------------------------------------------
    # SHOWS THAT BECAME BOOKABLE
    # -----------------------------------------------------

    became_bookable = []

    for key, show in current_state.items():

        old = previous_state.get(
            key
        )

        if not old:
            continue

        was_bookable = bool(
            old.get("bookable")
        )

        now_bookable = bool(
            show.get("bookable")
        )

        if (
            not was_bookable
            and now_bookable
        ):
            became_bookable.append(
                show
            )

    print(
        f"Shows that became bookable: "
        f"{len(became_bookable)}"
    )

    if became_bookable:

        notify_became_bookable(
            cfg,
            became_bookable
        )

        print(
            "Bookable-state Telegram notification sent."
        )

    # -----------------------------------------------------
    # SAVE STATE
    # -----------------------------------------------------

    save_json(
        STATE_PATH,
        {
            "shows": current_state,
            "checked_at": int(
                time.time()
            ),
        }
    )

    print(
        "State saved successfully."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
