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
    "music": r"music|concert|\bband\b|jazz|blues|zydeco|cajun jam|\bdj\b|symphony|orchestra|choir|singer|songwriter|fais do-do|dance ?hall|two-step|karaoke|open mic|bluegrass|country night|hip hop|\brap\b|rock\b|\bjam\b|acoustic|live on",
    "food": r"\bfood|dinner|brunch|lunch|breakfast|crawfish|boudin|gumbo|tasting|\bwine|\bbeer|brew|cocktail|\bchef|cooking|farmers market|\bfeast|supper|bbq|barbecue|pop-up|happy hour|cook-off|cookoff",
    "arts": r"\bart\b|\barts\b|museum|gallery|exhibit|theat(?:er|re)|comedy|comedian|\bfilm|movie|ballet|poetry|\bbook|author|artwalk|art walk|\bcraft|painting|broadway|musical|opera|improv",
    "wellness": r"yoga|\brun\b|\b5k\b|\b10k\b|marathon|half marathon|\bwalk\b|fitness|workout|meditation|sound bath|wellness|\bhealth|pilates|\bbike|cycling|\bhike|tai chi|blood drive|zumba|mental health|run club",
    "family": r"\bkids?\b|children|family|storytime|story time|toddler|\bteens?\b|sensory|playtime|\bcamp\b|santa|easter egg|trick or treat|\bbaby|preschool",
    "community": r"volunteer|clean ?up|fundraiser|benefit|charity|council|workshop|lecture|\btalk\b|seminar|networking|parade|\bfair\b|basketball|football|baseball|softball|volleyball|soccer|ragin|market|festival|\bclass\b",
}
CATEGORY_ORDER = list(CATEGORIES)


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
    s = strip_code(str(text or ""))
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


def categorize(main_text: str, extra_text: str = "", forced: list[str] | None = None) -> list[str]:
    """Categories come from the title, venue and the source's own tags. The description is only
    used when those say nothing (so a storytime that mentions "songs" doesn't become music)."""
    cats = [c for c in (forced or []) if c in CATEGORIES]
    for text in (main_text, extra_text):
        t = text.lower()
        for name, pattern in CATEGORIES.items():
            if name not in cats and re.search(pattern, t):
                cats.append(name)
        if cats:
            break
    if not cats:
        cats = ["community"]
    return sorted(cats, key=CATEGORY_ORDER.index)[:3]


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
    venue = clean(venue, 120) or src.get("venue", "")
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
        "cats": categorize(f"{title} {hint} {venue}", desc, src.get("cats")),
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
    events = [from_jsonld(src, x) for x in jsonld_events(page)]
    if not any(events):
        raise Skip("no event data found on the page")
    return events


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
    content = json.loads(raw)["choices"][0]["message"]["content"]
    items = json.loads(content).get("events", [])
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


def _form_fields(body: str) -> dict:
    fields = {}
    for m in re.finditer(r"###\s*(.+?)\n+(.*?)(?=\n###|\Z)", body or "", re.S):
        v = m.group(2).strip()
        fields[m.group(1).strip().lower()] = "" if v == "_No response_" else v
    return fields


def _time_24h(t: str) -> str | None:
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?", t or "", re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return f"{h:02d}:{m.group(2) or '00'}"
    m = re.match(r"\s*(\d{1,2}):(\d{2})", t or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def submitted_events(start, end):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise Skip("runs on GitHub only")
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    issues = json.loads(fetch(f"https://api.github.com/repos/{repo}/issues?labels=approved&state=open&per_page=100",
                              headers=headers))
    out = []
    for issue in issues:
        f = _form_fields(issue.get("body", ""))
        date = f.get("date (yyyy-mm-dd)") or f.get("date", "")
        st, et = _time_24h(f.get("start time", "")), _time_24h(f.get("end time", ""))
        cat = f.get("category", "").lower()
        src = {"name": "Submitted", "cats": [c for c in CATEGORIES if c in cat]}
        out.append(make_event(
            src, title=f.get("event name"), start=f"{date}T{st}" if st else date,
            end=f"{date}T{et}" if et else None, venue=f.get("venue", ""), address=f.get("address", ""),
            price=f.get("price", ""), url=f.get("link", "") or issue.get("html_url", ""),
            desc=f.get("description", "")))
    return out


# ---------- merge, dedupe, save ----------

def _key(e):
    t = re.sub(r"[^a-z0-9]", "", e["title"].lower())
    return f"{t[:32]}|{e['start'][:10]}"


def _richness(e):
    return sum(bool(e.get(k)) for k in ("end", "venue", "address", "price", "desc", "image")) + \
        (0 if e["allDay"] else 2)


def _same_event(a, b):
    """Same start time and one title contains the other ("Sound Bath" / "Sound Bath with Ann Kergan")."""
    if a["start"] != b["start"] or a["allDay"]:
        return False
    ta, tb = (re.sub(r"[^a-z0-9]", "", x["title"].lower()) for x in (a, b))
    short, long_ = sorted((ta, tb), key=len)
    return len(short) >= 6 and short in long_


def merge(events):
    best = {}
    by_start = {}
    for e in events:
        k = _key(e)
        if k not in best:
            twin = next((x for x in by_start.get(e["start"], []) if _same_event(x, e)), None)
            if twin is None:
                best[k] = e
                by_start.setdefault(e["start"], []).append(e)
                continue
            k = _key(twin)
        a = best[k]
        keep, other = (e, a) if _richness(e) > _richness(a) else (a, e)
        if keep is not a:
            by_start[a["start"]] = [keep if x is a else x for x in by_start[a["start"]]]
        for field in ("end", "venue", "address", "price", "desc", "image", "url"):
            if not keep.get(field) and other.get(field):
                keep[field] = other[field]
        keep["cats"] = sorted(set(keep["cats"]) | set(other["cats"]), key=CATEGORY_ORDER.index)[:3]
        also = set(keep.get("alsoOn", [])) | {other["source"]} | set(other.get("alsoOn", []))
        also.discard(keep["source"])
        keep["alsoOn"] = sorted(also)
        best[k] = keep
    return list(best.values())


RUNNERS = {"tribe": src_tribe, "jsonld": src_jsonld, "ical": src_ical, "saffire": src_saffire,
           "squarespace": src_squarespace, "downtown": src_downtown,
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

    sources = [s for s in json.loads(SOURCES_FILE.read_text(encoding="utf-8"))["sources"]
               if s.get("enabled", True) and only in s["name"].lower()]
    if not only:
        sources += [{"name": "Bon Temps", "type": "_manual"}, {"name": "Submitted", "type": "_submitted"}]

    all_events, report = [], []
    for src in sources:
        t0 = time.time()
        runner = {"_manual": lambda s, a, b: manual_events(a, b),
                  "_submitted": lambda s, a, b: submitted_events(a, b)}.get(src["type"]) or RUNNERS[src["type"]]
        try:
            found = [e for e in runner(src, start, end) if e]
            found = [e for e in found if (e["end"] or e["start"]) >= cutoff and e["start"] <= iso(end)]
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

    if only:  # partial runs don't overwrite the site's data
        print(f"\n(test run for '{only}': docs/events.json not changed)")
        return

    events = merge(all_events)
    events.sort(key=lambda e: (e["start"], e["title"]))
    for e in events:
        e["id"] = hashlib.sha1(_key(e).encode()).hexdigest()[:10]
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": report,
        "events": events,
    }, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"\nwrote {len(events)} events to {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv)
