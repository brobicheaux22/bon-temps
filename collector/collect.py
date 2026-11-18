"""Bon Temps event collector.

Checks every source in collector/sources.json (plus manual.json and approved
"Submit an event" issues), cleans and merges the events, and writes
docs/events.json, which the website loads.

Uses only the Python standard library, so there is nothing to install.

    python collector/collect.py            # check everything
    python collector/collect.py "Blue Moon" # check only sources whose name contains this

Optional environment variables:
    GITHUB_TOKEN          lets the AI read venue pages that have no data feed
                          (GitHub Actions provides this automatically)
    TICKETMASTER_API_KEY  free key from developer.ticketmaster.com
    GITHUB_REPOSITORY     owner/repo, used to read approved event submissions
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "collector" / "sources.json"
MANUAL_FILE = ROOT / "collector" / "manual.json"
OUT_FILE = ROOT / "docs" / "events.json"

DAYS_AHEAD = 60
USER_AGENT = "BonTemps/1.0 (community events guide for Lafayette, LA)"
AI_URL = "https://models.github.ai/inference/chat/completions"
AI_MODEL = os.environ.get("BONTEMPS_MODEL", "openai/gpt-4o-mini")
AI_PAGE_CHARS = 18000  # keeps each request inside the free tier's size limit

# Lafayette is at the center; events elsewhere are dropped when a city is given.
AREA = {
    "lafayette", "broussard", "youngsville", "scott", "carencro", "duson", "milton",
    "maurice", "breaux bridge", "st. martinville", "st martinville", "saint martinville",
    "cecilia", "arnaudville", "rayne", "sunset", "grand coteau", "opelousas", "abbeville",
    "new iberia", "church point", "erath", "kaplan", "crowley",
}

CATEGORIES = {
    "music": r"music(?!al)|concert|\bband\b|jazz|blues|zydeco|cajun jam|\bdj\b|symphony|orchestra|choir|singer|songwriter|fais do-do|dance ?hall|two-step|karaoke|open mic|bluegrass|country night|hip hop|\brap\b|rock\b|\bjam\b|acoustic|live on|, tba\b|\bw/ |\bft\b.|\bfeat\b.|album release",
    "food": r"\bfood|dinner|brunch|lunch|breakfast|crawfish|boudin|gumbo|tasting|\bwine|\bbeer|brew|cocktail|\bchef|cooking|farmers market|\bfeast|supper|bbq|barbecue|pop-up|happy hour|cook-off|cookoff",
    "arts": r"\bart\b|\barts\b|museum|gallery|exhibit|\bfilm|movie|poetry|\bbook|author|artwalk|art walk|\bcraft|painting|\bscreen(?:ing)?s?\b|film festival|selection of works|artist talk",
    "theater": r"theat(?:er|re)|musical|broadway|\ba play\b|stage play|new play|\bplays\b|shakespeare|playwright|ballet|nutcracker|opera\b|\bimprov\b|comedy|comedian|stand-up|drag show|cabaret|readers.? theat|on stage|\bthe wiz\b|saturday night live|\bsnl\b|late night with|netflix special|comedy central",
    "wellness": r"yoga|\brun\b|\b5k\b|\b10k\b|marathon|half marathon|\bwalk\b|fitness|workout|meditation|sound bath|wellness|\bhealth|pilates|\bbike|cycling|\bhike|tai chi|blood drive|zumba|mental health|run club",
    "family": r"\bkids?\b|children|family|storytime|story time|toddler|\bteens?\b|sensory|playtime|\bcamp\b|santa|easter egg|trick or treat|\bbaby|preschool",
    "community": r"volunteer|clean ?up|fundraiser|benefit|charity|council|workshop|lecture|\btalk\b|seminar|networking|parade|\bfair\b|basketball|football|baseball|softball|volleyball|soccer|ragin|market|festival|\bclass\b",
}
CATEGORY_ORDER = list(CATEGORIES)
THEATER_HINT = r"best musical|musical theat|broadway|theat(?:er|re) company|stage play|\bplaywright|\ba play\b|\bplay by\b|by william shakespeare|ballet company|touring production"


class Skip(Exception):
    """A source that can't run right now (for example, no API key)."""


# ---------- time (Lafayette is US Central, with daylight saving) ----------

def _central_offset_hours(utc_naive: datetime) -> int:
    y = utc_naive.year

    def nth_sunday(month: int, n: int) -> datetime:
        d = datetime(y, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    dst_start = nth_sunday(3, 2) + timedelta(hours=8)  # 2 AM CST
    dst_end = nth_sunday(11, 1) + timedelta(hours=7)   # 2 AM CDT
    return -5 if dst_start <= utc_naive < dst_end else -6


def to_central(d: datetime) -> datetime:
    """Aware datetime -> naive Lafayette local time."""
    u = d.astimezone(timezone.utc).replace(tzinfo=None)
    return u + timedelta(hours=_central_offset_hours(u))


def central_to_utc(d: datetime) -> datetime:
    guess = d + timedelta(hours=6)
    return d - timedelta(hours=_central_offset_hours(guess))


def now_central() -> datetime:
    return to_central(datetime.now(timezone.utc))


def parse_when(value) -> tuple[datetime | None, bool]:
    """Returns (local naive datetime, is_date_only)."""
    if not value:
        return None, False
    s = str(value).strip()
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if not m:
            return None, False
        d, date_only = datetime.fromisoformat(m.group(1)), True
    if d.tzinfo:
        d = to_central(d)
    return d, date_only


def iso(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M") if d else None


# ---------- helpers ----------

try:  # use an up-to-date certificate list when available (fixes some Windows setups)
    import certifi
    import ssl
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = None


def fetch(url: str, *, headers: dict | None = None, data: bytes | None = None, timeout: int = 40) -> str:
    h = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/calendar,*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


# WordPress page-builder "shortcodes" like [et_pb_text admin_label="..."] or [/et_pb_section].
# Quoted attribute values may themselves contain brackets, so they're matched as whole strings.
SHORTCODE = re.compile(r"""\[/?[a-z][a-z0-9_-]*(?:\s+[\w-]+(?:=(?:"[^"]*"|'[^']*'|[^\s\]]+))?)*\s*/?\]""")
MD_LINK = re.compile(r"\[([^\[\]]+)\]\((https?://[^)\s]+)\)")


def strip_code(s: str) -> str:
    for _ in range(3):                         # nested shortcodes
        s = SHORTCODE.sub(" ", s)
    s = MD_LINK.sub(r"\1", s)                  # [text](https://…) -> text
    s = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", s)  # template tags
    s = re.sub(r"\*\*|__|^#{1,6}\s+", "", s, flags=re.M)  # markdown bold and headings
    return s


def looks_like_code(s: str) -> bool:
    """True when text still reads like markup instead of a description."""
    return bool(re.search(r'\[/?[a-z_]+[\s\]]|[\w-]+="[^"]*"|</?[a-z]+[\s>]|\{"|":\s*"|\bfunction\s*\(|;\s*\}', s))


def clean(text, limit: int = 420, breaks: bool = False) -> str:
    s = strip_code(str(text or "")).replace("%26", "&")
    if breaks:  # keep paragraph breaks for event descriptions
        s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    if breaks:
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r" *\n *", "\n", s)
        # drop leftover button text like "BUY TICKETS HERE" or "Read more"
        s = re.sub(r"(?im)^(buy|get) tickets( here| now)?!?$|^(click here|read more|learn more|more info)\.?$", "", s)
        s = re.sub(r"\n{2,}", "\n\n", s).strip()
    else:
        s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def in_area(city: str) -> bool:
    c = city.strip().lower()
    return not c or c in AREA


# Words in venue names that say nothing about the event ("Acadiana Center for the Arts", "Moncus Theater")
VENUE_WORDS = r"(?i)theat(?:er|re)|performing|" + chr(92) + "barts?" + chr(92) + "b"


def categorize(main_text: str, extra_text: str = "", forced: list[str] | None = None,
               fallback: list[str] | None = None) -> tuple[list[str], bool]:
    """Categories come from the title, venue and the source's own tags. The description is only
    used when those say nothing (so a storytime that mentions "songs" doesn't become music).
    Returns (categories, guessed): guessed is True when nothing matched and the source's
    fallback (or "community") was used, so a better-informed duplicate can override it."""
    cats = [c for c in (forced or []) if c in CATEGORIES]
    for text in (main_text, extra_text):
        t = text.lower()
        for name, pattern in CATEGORIES.items():
            if name not in cats and re.search(pattern, t):
                cats.append(name)
        if cats:
            break
    # A show's description often says what it is even when the title doesn't ("The Wiz" -> "Best Musical")
    # (skipped for music-only sources like Blue Moon, whose band bios talk about "the musical heritage")
    if "theater" not in cats and "music" not in (forced or []) and re.search(THEATER_HINT, extra_text.lower()):
        cats.append("theater")
    # "Nutcracker Painting Class" or "The Nutcracker Tea" are themed outings, not stage shows
    main = main_text.lower()
    if ("theater" in cats and re.search(r"\b(?:class|classes|painting|paint|workshop|camp|tea|cake|cookies?)\b", main)
            and not re.search(r"theat(?:er|re)|musical|\bplay\b|comedy|improv|ballet\b(?! cake)", main)):
        cats.remove("theater")
    guessed = not cats
    if guessed:
        cats = list(fallback or ["community"])
    return sorted(cats, key=CATEGORY_ORDER.index)[:3], guessed


def price_info(raw) -> tuple[str, bool]:
    p = clean(raw, 60)
    free = bool(re.search(r"\bfree\b", p, re.I)) or p in {"0", "$0", "0.00", "$0.00"}
    return ("Free" if free else p), free


def make_event(src: dict, *, title, start, end=None, all_day=False, venue="", address="",
               price="", url="", desc="", image="", hint="") -> dict | None:
    title = clean(title, 140)
    s, date_only = parse_when(start)
    if not title or not s:
        return None
    e, _ = parse_when(end)
    if e and e <= s:
        e = None
    all_day = bool(all_day) or date_only
    price_text, free = price_info(price)
    desc = clean(desc, 1500, breaks=True)
    if looks_like_code(desc):  # couldn't clean it: show the "find more details" link instead
        desc = ""
    venue = clean(venue, 120)
    if re.fullmatch(r"(?i)(?:location|venue|place|event)(?: name)?|tb[adc]|to be (?:announced|determined)|n/?a|none|-+", venue):
        venue = ""  # calendar placeholders like Moncus Park's "Location Name"
    venue = venue or src.get("venue", "")
    cats, guessed = categorize(f"{title} {hint} {re.sub(VENUE_WORDS, '', venue)}", desc,
                               src.get("cats"), src.get("fallback"))
    return {
        "title": title,
        "start": iso(s),
        "end": iso(e),
        "allDay": all_day,
        "ongoing": bool(e and e - s >= timedelta(days=3)),  # exhibits, seasons, multi-day runs
        "venue": venue,
        "address": clean(address, 160),
        "price": price_text,
        "free": free,
        "cats": cats,
        "_guess": guessed,
        "desc": desc,
        "url": url or src.get("url", ""),
        "image": image if isinstance(image, str) and image.startswith("http") else "",
        "source": src["name"],
    }


# ---------- source: WordPress "The Events Calendar" ----------

def src_tribe(src, start, end):
    base = src["url"].rstrip("/") + "/wp-json/tribe/events/v1/events"
    url = base + "?" + urllib.parse.urlencode({
        "per_page": 50,
        "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date": end.strftime("%Y-%m-%d 23:59:59"),
    })
    out = []
    for _ in range(40):
        data = json.loads(fetch(url, timeout=90))
        for e in data.get("events", []):
            v = e.get("venue") if isinstance(e.get("venue"), dict) else {}
            city = (v.get("city") or "").strip()
            if not in_area(city):
                continue
            img = e.get("image") if isinstance(e.get("image"), dict) else {}
            out.append(make_event(
                src, title=e.get("title"), start=e.get("start_date"), end=e.get("end_date"),
                all_day=e.get("all_day"), venue=v.get("venue") or "",
                address=", ".join(x for x in [v.get("address"), city] if x),
                price=e.get("cost"), url=e.get("url"), desc=e.get("description"),
                image=img.get("url", ""),
                hint=" ".join(c.get("name", "") for c in e.get("categories", [])),
            ))
        url = data.get("next_rest_url")
        if not url:
            break
    return out


# ---------- source: structured event data built into a page (schema.org JSON-LD) ----------

def jsonld_events(page: str) -> list[dict]:
    found = []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                t = x.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(isinstance(y, str) and y.endswith("Event") for y in types):
                    found.append(x)
                for k in ("@graph", "itemListElement", "item", "event", "subEvent"):
                    if k in x:
                        stack.append(x[k])
    return found


def _undo_bad_offset(value):
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return value
    if not d.tzinfo:
        return value
    return (d.replace(tzinfo=None) - d.utcoffset()).isoformat()


def from_jsonld(src, x):
    loc = x.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    venue, address = "", ""
    if isinstance(loc, dict):
        venue = loc.get("name", "")
        a = loc.get("address")
        if isinstance(a, dict):
            if not in_area(a.get("addressLocality") or ""):
                return None
            address = ", ".join(v for v in [a.get("streetAddress"), a.get("addressLocality")] if v)
        elif isinstance(a, str):
            address = a
    elif isinstance(loc, str):
        venue = loc
    offers = x.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = ""
    if isinstance(offers, dict) and offers.get("price") not in (None, ""):
        p = str(offers["price"])
        price = "Free" if p in {"0", "0.0", "0.00"} else (p if p.startswith("$") else f"${p}")
    image = x.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    s, e = x.get("startDate"), x.get("endDate")
    if src.get("fixTimezone"):  # site labels local times as if they were UTC
        s, e = _undo_bad_offset(s), _undo_bad_offset(e)
    return make_event(src, title=x.get("name"), start=s, end=e,
                      venue=venue, address=address, price=price, url=x.get("url", ""),
                      desc=x.get("description", ""), image=image or "")


def src_jsonld(src, start, end):
    page = fetch(src["url"])
    items = jsonld_events(page)
    if src.get("details"):  # list pages only give dates: open each event's page for times and details
        items = [_with_details(x) for x in items]
    events = [from_jsonld(src, x) for x in items]
    if not any(events):
        raise Skip("no event data found on the page")
    return events


# Event pages already read are remembered between runs (collector/detail-cache.json), so each
# update only opens pages for events it hasn't seen before.
DETAIL_FILE = ROOT / "collector" / "detail-cache.json"
DETAILS: dict = {}
DETAILS_USED: set = set()


def _with_details(x):
    url = x.get("url")
    if not url:
        return x
    DETAILS_USED.add(url)
    if url not in DETAILS:
        try:
            found = jsonld_events(fetch(url))
            DETAILS[url] = found[0] if found else {}
        except (urllib.error.URLError, TimeoutError, ValueError):
            return x  # try again next run
        time.sleep(0.3)
    d = DETAILS[url]
    if not d:
        return x
    merged = {**x, **{k: v for k, v in d.items() if v not in (None, "", [], {})}}
    merged["url"] = url
    return merged


# ---------- source: iCal calendar feed ----------

def _ics_time(v: str, params: str = "") -> tuple[datetime | None, bool]:
    """iCal date/time -> (Lafayette local naive datetime, is_date_only)."""
    v = v.strip()
    if "VALUE=DATE" in params.upper() and "DATE-TIME" not in params.upper() or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d"), True
    m = re.match(r"(\d{8})T(\d{4})", v)
    if not m:
        return None, False
    d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    if v.endswith("Z"):
        d = to_central(d.replace(tzinfo=timezone.utc))
    return d, False  # times with a TZID are treated as local (Central) time


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _occurrences(first: datetime, rule: str, window_end: datetime) -> list[datetime]:
    """Expand a simple RRULE (daily, weekly, monthly) up to window_end."""
    r = dict(p.split("=", 1) for p in rule.strip().split(";") if "=" in p)
    freq, interval = r.get("FREQ", ""), int(r.get("INTERVAL", "1"))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = _ics_time(r["UNTIL"])[0] if "UNTIL" in r else None
    stop = min(window_end, until) if until else window_end
    days = [(re.match(r"(-?\d*)([A-Z]{2})", b).groups()) for b in r.get("BYDAY", "").split(",") if b]
    out, d = [], first
    if freq == "DAILY":
        while d <= stop and (count is None or len(out) < count):
            out.append(d)
            d += timedelta(days=interval)
    elif freq == "WEEKLY":
        wanted = sorted({_WEEKDAYS[w] for _, w in days} or {first.weekday()})
        week = first - timedelta(days=first.weekday())
        while week <= stop and (count is None or len(out) < count):
            for wd in wanted:
                x = week + timedelta(days=wd)
                if first <= x <= stop and (count is None or len(out) < count):
                    out.append(x)
            week += timedelta(weeks=interval)
    elif freq == "MONTHLY":
        y, mo = first.year, first.month
        while datetime(y, mo, 1) <= stop and (count is None or len(out) < count):
            if days:  # e.g. BYDAY=2SA (second Saturday) or -1FR (last Friday)
                for n, w in days:
                    hits = [datetime(y, mo, dd, first.hour, first.minute) for dd in range(1, 32)
                            if _valid(y, mo, dd) and datetime(y, mo, dd).weekday() == _WEEKDAYS[w]]
                    pick = hits[int(n) - 1 if int(n or 1) > 0 else int(n)] if hits and abs(int(n or 1)) <= len(hits) else None
                    if pick and first <= pick <= stop:
                        out.append(pick)
            elif _valid(y, mo, first.day):
                x = first.replace(year=y, month=mo)
                if x <= stop:
                    out.append(x)
            mo += interval
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
    else:
        out = [first]
    return out


def _valid(y, m, d):
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


def src_ical(src, start, end):
    text = fetch(src["url"], timeout=90)
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold long lines
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields: dict[str, list[tuple[str, str]]] = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            value = value.strip().replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")
            fields.setdefault(name.upper(), []).append((value, params))
        get = lambda k: fields.get(k, [("", "")])[0][0]
        if get("STATUS").upper() == "CANCELLED" or "DTSTART" not in fields:
            continue
        first, date_only = _ics_time(*fields["DTSTART"][0])
        if not first:
            continue
        last = _ics_time(*fields["DTEND"][0])[0] if "DTEND" in fields else None
        length = (last - first) if last and last > first else None

        starts = _occurrences(first, get("RRULE"), end) if "RRULE" in fields else [first]
        for v, p in fields.get("RDATE", []):
            starts += [d for d in (_ics_time(x, p)[0] for x in v.split(",")) if d]
        skip = {d for v, p in fields.get("EXDATE", []) for d in (_ics_time(x, p)[0] for x in v.split(","))}

        desc_html = get("DESCRIPTION")
        img = get("X-WP-IMAGES-URL").split(";")[0] or (re.findall(r"<img[^>]+src='([^']+)'", desc_html) or [""])[0]
        location = get("LOCATION")
        cost = get("X-COST")  # X-COST-TYPE isn't used: some calendars mark every event "free" by default
        venue, _, addr = location.partition(" @ ")  # "Blue Moon Saloon @ 215 East Convent Street"
        for s in sorted(set(starts) - skip):
            e = s + length if length else None
            if e and e.strftime("%H:%M") == "23:59":  # placeholder "until midnight" end time
                e = None
            if (e or s) < start or s > end:
                continue
            fmt = "%Y-%m-%d" if date_only else "%Y-%m-%dT%H:%M"
            out.append(make_event(
                src, title=get("SUMMARY"), start=s.strftime(fmt), end=e.strftime(fmt) if e and not date_only else None,
                venue=(venue if addr else location.split(",")[0]) if location else "",
                address=addr or location,
                price=cost, url=get("URL") or src.get("link", ""), desc=desc_html, image=img,
            ))
    return out


# ---------- source: Ticketmaster (Cajundome, Heymann and other ticketed shows) ----------

def src_ticketmaster(src, start, end):
    key = os.environ.get("TICKETMASTER_API_KEY")
    if not key:
        raise Skip("add a free TICKETMASTER_API_KEY to turn this on")
    out = []
    for page in range(5):
        url = "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode({
            "apikey": key, "latlong": "30.2241,-92.0198", "radius": "30", "unit": "miles",
            "size": "200", "page": page, "sort": "date,asc",
            "startDateTime": central_to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": central_to_utc(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        data = json.loads(fetch(url))
        for e in data.get("_embedded", {}).get("events", []):
            d = e.get("dates", {}).get("start", {})
            when = d.get("localDate", "")
            if d.get("localTime") and not d.get("timeTBA"):
                when += "T" + d["localTime"][:5]
            venues = e.get("_embedded", {}).get("venues", [{}])
            v = venues[0] if venues else {}
            city = (v.get("city") or {}).get("name", "")
            if not in_area(city):
                continue
            pr = (e.get("priceRanges") or [{}])[0]
            price = ""
            if pr.get("min") is not None:
                lo, hi = round(pr["min"]), round(pr.get("max") or pr["min"])
                price = f"${lo}" if lo == hi else f"${lo}–${hi}"
            imgs = sorted(e.get("images", []), key=lambda i: -(i.get("width") or 0))
            genre = " ".join(
                (c.get(k) or {}).get("name", "") for c in e.get("classifications", [])
                for k in ("segment", "genre", "subGenre"))
            out.append(make_event(
                src, title=e.get("name"), start=when, venue=v.get("name", ""),
                address=", ".join(x for x in [(v.get("address") or {}).get("line1"), city] if x),
                price=price, url=e.get("url", ""), desc=e.get("info") or e.get("pleaseNote") or "",
                image=imgs[0]["url"] if imgs else "",
                hint=genre.replace("Music", "music").replace("Arts & Theatre", "theater").replace("Sports", "basketball"),
            ))
        if page + 1 >= data.get("page", {}).get("totalPages", 1):
            break
        time.sleep(0.3)
    return out


# ---------- source: Squarespace event pages (Hideaway on Lee) ----------

def src_squarespace(src, start, end):
    data = json.loads(fetch(src["url"].split("?")[0] + "?format=json"))
    base = src["url"].split("/", 3)
    origin = "/".join(base[:3])
    out = []
    for e in data.get("upcoming", []) + data.get("items", []):
        title = e.get("title", "")
        if not e.get("startDate") or re.search(r"\bclosed\b", title, re.I):
            continue
        utc = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        loc = e.get("location") or {}
        out.append(make_event(
            src, title=title, start=to_central(utc(e["startDate"])).isoformat(),
            end=to_central(utc(e["endDate"])).isoformat() if e.get("endDate") else None,
            venue=loc.get("addressTitle") or "", address=loc.get("addressLine1") or "",
            url=origin + e["fullUrl"] if e.get("fullUrl", "").startswith("/") else src["url"],
            desc=e.get("excerpt") or e.get("body") or "", image=e.get("assetUrl") or ""))
    return out


# ---------- source: a listing page that links to one page per event (Wix Events: Cité des Arts) ----------

def _read_event_pages(src, urls, limit=45):
    """Open each event page and read the event data built into it."""
    out = []
    for u in urls[:limit]:
        try:
            page = fetch(u)
        except urllib.error.URLError:
            continue
        for x in jsonld_events(page):
            x.setdefault("url", u)
            out.append(from_jsonld(src, x))
        time.sleep(0.4)
    return out


def src_eventlinks(src, start, end):
    """Collect event links from the listing page(s), then read each event page."""
    pattern = re.compile(src["links"])
    urls = []
    for listing in [src["url"], *src.get("more", [])]:
        for u in pattern.findall(fetch(listing)):
            if u not in urls:
                urls.append(u)
    return _read_event_pages(src, urls)


def src_wixsitemap(src, start, end):
    """Wix sites list every event page in a sitemap, even when the calendar itself loads with
    JavaScript (Wonderland, Cité des Arts). Event addresses usually end with their date
    ("...-2026-11-20-19-30"), so only upcoming ones are opened."""
    xml = fetch(src["url"])
    entries = re.findall(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", xml)
    dated, undated = [], []
    recent = (start - timedelta(days=120)).strftime("%Y-%m-%d")
    for loc, lastmod in entries:
        m = re.search(r"-(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}(?:-\d+)?/?$", loc)
        if m:
            day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d"):
                dated.append((day, loc))
        elif (lastmod or "")[:10] >= recent:
            undated.append(loc)  # one-off pages without a date in the address: check recent ones
    urls = [u for _, u in sorted(dated)] + undated[:15]
    return _read_event_pages(src, urls, limit=src.get("limit", 45))


def src_mecrest(src, start, end):
    """WordPress 'Modern Events Calendar' sites (Acadiana Center for the Arts) list every event
    page through WordPress's public API; each page has the event's details built in."""
    api = src["url"].rstrip("/") + "/wp-json/wp/v2/mec-events?per_page=50&orderby=modified&_fields=link"
    urls = [x["link"] for x in json.loads(fetch(api)) if x.get("link")]
    return _read_event_pages(src, urls, limit=src.get("limit", 45))


# ---------- source: Saffire venue sites (Heymann Center) ----------

def src_saffire(src, start, end):
    base = src["url"].rstrip("/") + "/services/eventsservice.asmx/"
    h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    flags = {"categoryID": 0, "tagID": 0, "keywords": "", "isFeatured": "false", "fanPicks": "false",
             "pastEvents": "false", "allEvents": "false", "memberEvents": "false", "memberOnly": "false",
             "showCategoryExceptionID": 0, "isolatedSchedule": 0, "customFieldFilters": []}
    us = lambda d: f"{d.month}/{d.day}/{d.year}"
    days = json.loads(fetch(base + "GetEventDays", headers=h, data=json.dumps({
        **flags, "day": "", "startDate": us(start), "endDate": us(end), "currentUserItems": "false",
        "myPicks": "false", "searchInDescription": "false"}).encode()))["d"]
    out = []
    for i in range(0, len(days), 10):
        data = json.loads(fetch(base + "GetEventDaysByList", headers=h, data=json.dumps({
            **flags, "dates": ",".join(days[i:i + 10]), "day": "", "searchInDescription": False}).encode()))["d"]
        for day in data.get("Days", []):
            for slot in day.get("Times", []):
                for it in slot.get("Items", []):
                    if it.get("IsPrivate") or it.get("IsMemberOnly"):
                        continue
                    when = f"{day['Year']:04d}-{day['Month']:02d}-{day['Day']:02d}"
                    t = it.get("Time") if it.get("TimeIsSpecified") else None
                    if t is not None:
                        when += f"T{t // 100:02d}:{t % 100:02d}"
                    out.append(make_event(
                        src, title=it.get("Name"), start=when, url=it.get("DetailURL") or src["url"],
                        desc=it.get("ShortDescription") or it.get("LongDescription") or "",
                        image=it.get("ImageOrVideoThumbnailWithPath") or ""))
    return out


# ---------- source: Downtown Lafayette (Webflow event pages) ----------

def src_downtown(src, start, end):
    listing = fetch(src["url"])
    links = sorted(set(re.findall(r'href="(/event/[^"#?]+)"', listing)))[:40]
    out = []
    for path in links:
        url = urllib.parse.urljoin(src["url"], path)
        try:
            page = fetch(url)
        except urllib.error.URLError:
            continue
        title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        lines = [l.strip() for l in page_text(page, url).split("\n")]
        lines = [re.sub(r"\s*\[https?://[^\]]+\]\s*", " ", l).strip() for l in lines if l.strip()]
        # the page lists machine-readable values: 2026-11-21 / 16:30 / December 31, 2026 / 23:00
        idx = next((i for i, l in enumerate(lines) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", l)), None)
        if idx is None or not title:
            continue
        start_s, end_s = lines[idx], None
        rest = lines[idx + 1: idx + 4]
        if rest and re.fullmatch(r"\d{1,2}:\d{2}", rest[0]):
            start_s += "T" + rest.pop(0).zfill(5)
        if rest and re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, \d{4}", rest[0]):
            end_d = datetime.strptime(rest.pop(0), "%B %d, %Y").strftime("%Y-%m-%d")
            end_s = end_d + ("T" + rest[0].zfill(5) if rest and re.fullmatch(r"\d{1,2}:\d{2}", rest[0]) else "")
        try:
            attend = lines.index("Attend")
        except ValueError:
            attend = idx
        block = lines[max(0, attend - 6):attend]
        desc = max(block, key=len) if block else ""
        venue = next((block[i - 1] for i, l in enumerate(block) if l == "|" and i > 0), "")
        address = next((block[i + 1] for i, l in enumerate(block) if l == "|" and i + 1 < len(block)), "")
        out.append(make_event(src, title=clean(title.group(1)), start=start_s, end=end_s,
                              venue=venue, address=address, url=url, desc=desc))
        time.sleep(0.5)
    return out


# ---------- source: any web page, read by AI (free GitHub Models) ----------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "time"}

    def __init__(self, base):
        super().__init__()
        self.base, self.parts, self.skip = base, [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                self.parts.append(f" [{urllib.parse.urljoin(self.base, href)}] ")
        if tag == "time":
            dt = dict(attrs).get("datetime")
            if dt:
                self.parts.append(f" ({dt}) ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_text(page: str, base: str) -> str:
    p = _TextExtractor(base)
    p.feed(page)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


AI_PROMPT = """You extract upcoming events from a venue's web page for a Lafayette, Louisiana events guide.
Today is {today}. Return JSON: {{"events": [...]}}. Each event:
{{"title": str, "start": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" if no time, "end": same format or null,
 "venue": str or null, "price": str or null, "url": str or null, "description": short str or null}}
Rules: only real dated events from {today} through {until}. If a date has no year, use the next
occurrence after today. Times are local. Don't invent anything that isn't on the page. Skip
"hours of operation", menus and closed notices. For repeating events (like "every Wednesday"),
list each date in range. Use the page's link for the event's url when there is one.
The page is from: {name} ({url})"""


def ai_extract(src, text, start, end):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_MODELS_TOKEN")
    if not token:
        raise Skip("this page needs AI reading, which runs on GitHub (no GITHUB_TOKEN here)")
    body = {
        "model": AI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": AI_PROMPT.format(
                today=start.strftime("%A %Y-%m-%d"), until=end.strftime("%Y-%m-%d"),
                name=src["name"], url=src["url"])},
            {"role": "user", "content": text[:AI_PAGE_CHARS]},
        ],
    }
    for attempt in range(3):
        try:
            raw = fetch(AI_URL, data=json.dumps(body).encode(), timeout=120, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json"})
            break
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < 2:  # free tier rate limit: wait and retry
                time.sleep(30 * (attempt + 1))
                continue
            raise
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError):
        raise RuntimeError(f"unexpected AI response: {raw[:200]!r}")
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())  # tolerate ```json fences
    try:
        items = json.loads(content).get("events", [])
    except ValueError:
        raise RuntimeError(f"AI reply wasn't JSON: {content[:200]!r}")
    return [make_event(src, title=i.get("title"), start=i.get("start"), end=i.get("end"),
                       venue=i.get("venue") or "", price=i.get("price") or "",
                       url=i.get("url") or "", desc=i.get("description") or "")
            for i in items if isinstance(i, dict)]


def src_page(src, start, end):
    page = fetch(src["url"])
    structured = [from_jsonld(src, x) for x in jsonld_events(page)]
    if any(structured):
        return structured
    text = page_text(page, src["url"])
    if len(text) < 200:
        raise Skip("page has almost no text (it may load events with JavaScript)")
    # Only spend an AI request when the page changed since last time (saves the free daily quota).
    digest = hashlib.sha1(text[:AI_PAGE_CHARS].encode()).hexdigest()[:12]
    PAGE_HASHES[src["name"]] = digest
    if LAST_RUN["hashes"].get(src["name"]) == digest and src["name"] in LAST_RUN["events"]:
        return [e for e in LAST_RUN["events"][src["name"]]]
    events = ai_extract(src, text, start, end)
    time.sleep(5)  # stay under the free tier's requests-per-minute limit
    return events


PAGE_HASHES: dict[str, str] = {}
LAST_RUN: dict = {"hashes": {}, "events": {}}


# ---------- your own events: manual.json and approved submissions ----------

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def manual_events(start, end):
    if not MANUAL_FILE.exists():
        return []
    out = []
    for m in json.loads(MANUAL_FILE.read_text(encoding="utf-8")).get("events", []):
        s = {"name": m.get("source") or "Bon Temps", "cats": m.get("cats")}
        common = dict(venue=m.get("venue", ""), address=m.get("address", ""), price=m.get("price", ""),
                      url=m.get("url", ""), desc=m.get("desc", ""), image=m.get("image", ""))
        if m.get("weekly"):  # e.g. "weekly": "Wed", "time": "19:00", "endTime": "22:00"
            day = DAY_NAMES.index(m["weekly"][:3].lower())
            d = start.replace(hour=0, minute=0, second=0, microsecond=0)
            d += timedelta(days=(day - d.weekday()) % 7)
            while d <= end:
                ds = d.strftime("%Y-%m-%d")
                out.append(make_event(s, title=m["title"],
                                      start=f"{ds}T{m['time']}" if m.get("time") else ds,
                                      end=f"{ds}T{m['endTime']}" if m.get("endTime") else None, **common))
                d += timedelta(days=7)
        else:
            out.append(make_event(s, title=m.get("title"), start=m.get("start"), end=m.get("end"),
                                  all_day=m.get("allDay"), **common))
    return out


def _time_24h(t: str) -> str | None:
    """'8 PM', '8:30pm', '8:00:00 PM', '20:00' or '20:00:00' -> '20:00'."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap])\.?m?\.?", t or "", re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return f"{h:02d}:{m.group(2) or '00'}"
    m = re.match(r"\s*(\d{1,2}):(\d{2})", t or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _sheet_date(d: str) -> str | None:
    """'2026-11-21', '11/21/2026' or '11/21' (Google Sheets may reformat dates) -> '2026-11-21'."""
    d = (d or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return d
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", d)
    if not m:
        return None
    year = int(m.group(3)) if m.group(3) else now_central().year
    year += 2000 if year < 100 else 0
    return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


APPROVED = {"yes", "y", "x", "approved", "approve", "ok", "true", "✓", "✔"}


def src_sheet(src, start, end):
    """Events people submitted through the site's form. Only rows with Approved = yes are used."""
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(fetch(src["url"]))))
    out = []
    for raw in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if r.get("approved", "").lower() not in APPROVED:
            continue
        date = _sheet_date(r.get("date", ""))
        if not date:
            continue
        st, et = _time_24h(r.get("start time", "")), _time_24h(r.get("end time", ""))
        cat = r.get("category", "").lower()
        s = {"name": src["name"], "cats": [c for c in CATEGORIES if c in cat]}
        link = r.get("link", "")
        if link and not link.startswith("http"):
            link = "https://" + link
        out.append(make_event(
            s, title=r.get("event name"), start=f"{date}T{st}" if st else date,
            end=f"{date}T{et}" if et else None, venue=r.get("venue", ""), address=r.get("address", ""),
            price=r.get("price", ""), url=link, desc=r.get("description", "")))
    return out


# ---------- merge, dedupe, save ----------

def _key(e):
    t = re.sub(r"[^a-z0-9]", "", e["title"].lower())
    if e.get("ongoing") and e.get("end"):  # exhibits: sources disagree on the start, not the closing date
        return f"{t[:32]}|until {e['end'][:10]}"
    return f"{t[:32]}|{e['start'][:10]}"


def _richness(e):
    return sum(bool(e.get(k)) for k in ("end", "venue", "address", "price", "desc", "image")) + \
        (0 if e["allDay"] else 2)


def _same_event(a, b):
    """Same day, one title contains the other ("Sound Bath" / "Sound Bath with Ann Kergan",
    "Supercollider" / "Supercollider! an improv comedy showcase"), and the same start time
    unless one listing only gives the date."""
    if a["start"][:10] != b["start"][:10]:
        return False
    if not (a["allDay"] or b["allDay"]) and a["start"] != b["start"]:
        return False
    ta, tb = (re.sub(r"[^a-z0-9]", "", x["title"].lower()) for x in (a, b))
    short, long_ = sorted((ta, tb), key=len)
    return len(short) >= 6 and short in long_


def merge(events):
    best = {}     # slot key -> the best copy so far
    by_day = {}   # day -> slot keys, for matching near-identical titles
    for e in events:
        k = _key(e)
        day = e["start"][:10]
        if k not in best:
            twin = next((s for s in by_day.get(day, []) if _same_event(best[s], e)), None)
            if twin is None:
                best[k] = e
                by_day.setdefault(day, []).append(k)
                continue
            k = twin  # same event under a slightly different title: merge into its slot
        a = best[k]
        keep, other = (e, a) if _richness(e) > _richness(a) else (a, e)
        for field in ("end", "venue", "address", "price", "desc", "image", "url"):
            if field == "address" and keep.get("venue") and venue_key(keep["venue"]) != venue_key(other.get("venue", "")):
                continue  # don't pin another listing's address on a different venue
            if not keep.get(field) and other.get(field):
                keep[field] = other[field]
        # A guessed category (from a source's fallback) gives way to one that actually matched
        kg, og = keep.get("_guess", False), other.get("_guess", False)
        if kg and not og:
            cats = set(other["cats"])
        elif og and not kg:
            cats = set(keep["cats"])
        elif kg and og:  # both guesses: trust the more specific source (listed earlier in sources.json)
            first = min((keep, other), key=lambda x: x.get("_order", 999))
            cats = set(first["cats"])
        else:
            cats = set(keep["cats"]) | set(other["cats"])
        keep["cats"] = sorted(cats, key=CATEGORY_ORDER.index)[:3]
        keep["_guess"] = kg and og
        keep["_order"] = min(keep.get("_order", 999), other.get("_order", 999))
        also = set(keep.get("alsoOn", [])) | {other["source"]} | set(other.get("alsoOn", []))
        also.discard(keep["source"])
        keep["alsoOn"] = sorted(also)
        best[k] = keep
    return list(best.values())


# ---------- venue names for listings that only give an address ----------

ADDRESS_LIKE = re.compile(r"^\s*\d+[A-Za-z]?\s+\S")
_DIRECTIONS = {"e", "w", "n", "s", "east", "west", "north", "south", "e.", "w.", "n.", "s."}


def street_key(text: str) -> str:
    """'215 E Convent St, Lafayette' -> '215 convent'"""
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    if len(words) < 2 or not words[0].isdigit():
        return ""
    rest = [w for w in words[1:] if w not in _DIRECTIONS and not w.isdigit()]  # "2801-2851 Johnston"
    return f"{words[0]} {rest[0]}" if rest else ""


def resolve_venues(events, known: dict):
    """Swap 'venue = street address' for the venue's name, using (1) your list in sources.json,
    (2) what other listings say lives at that address, (3) a '📍 Name' line in the description."""
    names = {street_key(k): v for k, v in known.items() if street_key(k)}
    for e in events:  # learn from listings that give both a name and an address
        if e["venue"] and not ADDRESS_LIKE.match(e["venue"]):
            k = street_key(e.get("address", ""))
            if k and k not in names:
                names[k] = e["venue"]
    fixed = 0
    for e in events:
        if not ADDRESS_LIKE.match(e["venue"] or ""):
            continue
        k = street_key(e["venue"]) or street_key(e.get("address", ""))
        name = names.get(k)
        if not name:
            pin = re.search(r"📍\s*([^\n|]{3,60})", e.get("desc", ""))
            if pin and not ADDRESS_LIKE.match(pin.group(1)):
                name = pin.group(1).strip(" -–,.")
        if name:
            if not e.get("address"):
                e["address"] = e["venue"]
            e["venue"] = name
            fixed += 1
    return fixed


# ---------- quality check for events only an aggregator lists ----------

def keep_listing(e, cfg, known_venues: set) -> bool:
    """Events that only an aggregator (Happening Next) lists must be at a known venue, be in a
    'going out' category, or be a recognizable public event. Medicare seminars and realty
    parties at an office address don't make it."""
    agg = cfg.get("aggregators", [])
    sources = [e["source"], *e.get("alsoOn", [])]
    if not all(any(s.startswith(a) for a in agg) for s in sources):
        return True  # a venue's own calendar lists it
    if set(e["cats"]) & set(cfg.get("keepCats", [])):
        return True
    if re.search(cfg.get("keepWords", "$^"), e["title"], re.I):
        return True
    return venue_key(e["venue"]) in known_venues


def venue_key(v: str) -> str:
    v = re.split(r" - | \| |,| @ |\[", (v or "").lower())[0]
    v = re.sub(r"\b(?:the|and|guest|house|de|la|lafayette|center|centre|for|arts|performing|of|at)\b", " ", v)
    return re.sub(r"[^a-z0-9]", "", v)


RUNNERS = {"tribe": src_tribe, "jsonld": src_jsonld, "ical": src_ical, "saffire": src_saffire,
           "squarespace": src_squarespace, "downtown": src_downtown, "sheet": src_sheet, "eventlinks": src_eventlinks,
           "wixsitemap": src_wixsitemap, "mecrest": src_mecrest,
           "ticketmaster": src_ticketmaster, "page": src_page}


def main(argv):
    only = argv[1].lower() if len(argv) > 1 else ""
    start = now_central().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    end = start + timedelta(days=DAYS_AHEAD)
    cutoff = iso(start)

    previous = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for e in prev.get("events", []):
                for name in [e["source"], *e.get("alsoOn", [])]:
                    previous.setdefault(name, []).append({**e, "source": name, "alsoOn": []})
            LAST_RUN["hashes"] = {s["name"]: s["hash"] for s in prev.get("sources", []) if s.get("hash")}
            LAST_RUN["events"] = previous
        except ValueError:
            pass

    if DETAIL_FILE.exists():
        try:
            DETAILS.update(json.loads(DETAIL_FILE.read_text(encoding="utf-8")))
        except ValueError:
            pass

    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = [s for s in config["sources"] if s.get("enabled", True) and only in s["name"].lower()]
    if not only:
        sources += [{"name": "Bon Temps", "type": "_manual"}]

    all_events, report = [], []
    for order, src in enumerate(sources):
        t0 = time.time()
        runner = {"_manual": lambda s, a, b: manual_events(a, b)}.get(src["type"]) or RUNNERS[src["type"]]
        try:
            found = [e for e in runner(src, start, end) if e]
            for e in found:
                e["_order"] = order
            found = [e for e in found if (e["end"] or e["start"]) >= cutoff and e["start"] <= iso(end)]
            if src.get("maxDays"):  # open-ended "weekly trivia since 2022" style listings
                limit = timedelta(days=src["maxDays"])
                found = [e for e in found if not e["end"] or parse_when(e["end"])[0] - parse_when(e["start"])[0] <= limit]
            status = {"name": src["name"], "ok": True, "count": len(found)}
        except Skip as why:
            found, status = [], {"name": src["name"], "ok": None, "count": 0, "note": str(why)}
        except Exception as err:  # keep last run's events if a site is down
            kept = [e for e in previous.get(src["name"], []) if (e.get("end") or e["start"]) >= cutoff]
            found = kept
            status = {"name": src["name"], "ok": False, "count": len(kept),
                      "note": f"{type(err).__name__}: {str(err)[:140]}" + (" (showing last good results)" if kept else "")}
        if status["ok"] and src["name"] in PAGE_HASHES:
            status["hash"] = PAGE_HASHES[src["name"]]
        all_events += found
        report.append(status)
        mark = {True: "ok  ", None: "skip", False: "FAIL"}[status["ok"]]
        print(f"{mark} {src['name']:<42} {status['count']:>4} events  {time.time() - t0:5.1f}s  {status.get('note', '')}")

    # Remember event pages read this run; a full run also forgets pages no longer listed
    keep = DETAILS if only else {u: v for u, v in DETAILS.items() if u in DETAILS_USED}
    DETAIL_FILE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")

    if only:  # partial runs don't overwrite the site's data
        print(f"\n(test run for '{only}': docs/events.json not changed)")
        return

    fixed = resolve_venues(all_events, config.get("venueNames", {}))
    print(f"named {fixed} venues that were only an address")

    # Leave out clutter ("Moncus Mondays-Free Parking", listings that are just a venue's name)
    hide = config.get("hide", {}).get("patterns", [])
    if hide:
        hidden = re.compile("|".join(f"(?:{p})" for p in hide), re.I)
        before = len(all_events)
        all_events = [e for e in all_events if not hidden.search(e["title"].strip())]
        print(f"hid {before - len(all_events)} clutter listings")

    events = merge(all_events)

    # Venues on your address list always show that address (listing sites sometimes get it wrong)
    canon = {venue_key(name): f"{street}, Lafayette, LA" for street, name in config.get("venueNames", {}).items()}
    for e in events:
        right = canon.get(venue_key(e["venue"]))
        if right and street_key(e.get("address", "")) != street_key(right):
            e["address"] = right

    quality = config.get("quality")
    if quality:
        agg = tuple(quality.get("aggregators", []))
        known = {venue_key(e["venue"]) for e in events if not e["source"].startswith(agg)}
        known |= {venue_key(v) for v in config.get("venueNames", {}).values()}
        known |= {venue_key(v) for v in quality.get("knownVenues", [])}
        known.discard("")
        dropped = [e for e in events if not keep_listing(e, quality, known)]
        events = [e for e in events if keep_listing(e, quality, known)]
        print(f"left out {len(dropped)} low-quality aggregator listings")
        for e in dropped:  # shown in the Actions log, to spot anything worth adding back
            print(f"   - {e['start'][:10]} {e['title'][:60]} @ {e['venue'][:40]}")
    events.sort(key=lambda e: (e["start"], e["title"]))
    for e in events:
        e["id"] = hashlib.sha1(_key(e).encode()).hexdigest()[:10]
        e.pop("_guess", None)
        e.pop("_order", None)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": report,
        "events": events,
    }, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"\nwrote {len(events)} events to {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv)
