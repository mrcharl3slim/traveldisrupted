"""China Railway, via 12306. Timetables, no fares — and a seam to replace.

WHAT THIS IS AND IS NOT. 12306.cn is the only official seller of China Railway
tickets and has never authorised a third party, so there is no sanctioned
public API to call. What exists is the endpoint the site's own front end uses,
and this port speaks it: real departures, real arrivals, real seat classes,
read-only. That is enough for the engine, which asks a rail port for one thing
-- ways of being somewhere by a deadline -- and prices nothing itself.

It is enough, and it is not a licence. Treat this as a demo source: fine for
showing the arithmetic on real times, not something to sell tickets through.
`offers` is the whole contract, so swapping this body for a partner API
(Trip.com's rail API sells China Railway under a commercial agreement) changes
this file and nothing above it.

NO FARES, SAID OUT LOUD. The query returns availability by seat class, not a
price, exactly as transport.opendata.ch returns none for Switzerland. So the
same rule applies and for the same reason: every Offer carries
`price_source="estimate"`, the page says the fare is estimated, and the
traveller sees the true number in 12306's own checkout before paying. A
product whose pitch is honesty about what it can reach does not get to
quietly invent a fare.

TELECODES ARE FETCHED, NOT TYPED. 12306 keys stations by a three-letter
telecode -- Shanghai Hongqiao is AOH, Beijing South is VNP -- and a table of
them typed from memory is exactly the kind of quiet wrongness that sends a
traveller to the wrong city. The official list is published as a JavaScript
file the site loads on every visit; this port reads that and builds the map
itself, so a station it cannot resolve is a named refusal rather than a guess.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from base import PortError, call, get_json, get_text, url_for

HOST = "https://kyfw.12306.cn"

#: The station list the site itself loads: entries look like
#: "@bjb|北京北|VAP|beijingbei|bjb|0" -- marker, Chinese name, telecode, full
#: pinyin, initials, index.
STATIONS_URL = f"{HOST}/otn/resources/js/framework/station_name.js"

#: One query, the one the front end makes. Left-ticket means "what is still
#: for sale", which is the question a stranded traveller is asking.
QUERY_URL = f"{HOST}/otn/leftTicket/query"

#: G is high speed, D is the slower electric multiple unit. Both are trains a
#: traveller can be on by a deadline; anything slower is not a recovery from
#: this afternoon.
TRAIN_TYPES = "GD"

#: WHERE THE ROW'S FIELDS LIVE. The response packs each train into a
#: pipe-delimited string rather than an object, and these are the positions the
#: site's own script reads. They are observed, not documented, which is the
#: whole risk of this port: if 12306 reshuffles them, every row here becomes
#: nonsense rather than an error. `_row` therefore validates what it reads --
#: a train code that does not look like one, or a time that does not parse,
#: drops the row instead of publishing it.
FIELD = {"code": 3, "from": 6, "to": 7, "depart": 8, "arrive": 9,
         "duration": 10, "date": 13}

#: Second-class fares on the busiest corridors run roughly CNY 450-650; the
#: midpoint, converted at the declared rate. An estimate of an estimate, and
#: labelled as one wherever it appears -- see the module docstring.
import money

ESTIMATE = money.to_home(550.0, "CNY")[0]
DEEP_LINK = "https://www.12306.cn/en/"

#: At most this long between the query moment and a departure worth offering.
#: A train the day after tomorrow is not an answer to being stranded now, and
#: the caller filters again to its own window.
HORIZON = timedelta(hours=36)


def stations() -> dict[str, str]:
    """Pinyin and Chinese station name -> telecode, from 12306's own list.

    Both spellings are keyed because the engine holds "Shanghai Hongqiao" and
    the timetable answers in Chinese. Missing is missing: a name absent here
    resolves to nothing and `offers` refuses the search rather than sending a
    traveller to whichever station sorted first.
    """
    raw = call("chinarail_stations", {"list": "all"},
               lambda: {"js": get_text(STATIONS_URL)})
    text = raw.get("js") if isinstance(raw, dict) else str(raw)
    found: dict[str, str] = {}
    for entry in str(text or "").split("@"):
        parts = entry.split("|")
        if len(parts) < 5 or not parts[2]:
            continue
        telecode = parts[2].strip().upper()
        for name in (parts[1], parts[3]):        # Chinese, then full pinyin
            if name:
                found[_norm(name)] = telecode
    if not found:
        raise PortError("chinarail: the published station list did not parse")
    return found


def _norm(name: str) -> str:
    """Station names, comparable. "Shanghai Hongqiao" and "shanghaihongqiao"
    are the same platform written by two different systems."""
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


#: The compass, in the spelling 12306 files it under. Chinese stations are
#: named by direction -- there are five main stations in Beijing and the
#: direction IS the name -- and the published pinyin keeps it in Chinese:
#: Beijing South is "beijingnan", never "beijing south". Without this the
#: engine's own readable labels resolve to nothing and every Chinese rail
#: search refuses a station that is plainly in the list.
COMPASS = {"north": "bei", "south": "nan", "east": "dong", "west": "xi"}


def _pinyin(name: str) -> str:
    """A readable English station name -> the pinyin key 12306 lists it under."""
    words = re.split(r"[^a-z0-9]+", (name or "").lower())
    return "".join(COMPASS.get(word, word) for word in words if word)


def _telecode(telecodes: dict[str, str], name: str) -> str | None:
    """Either spelling, or nothing. Nothing is a refusal, never a guess."""
    return telecodes.get(_norm(name)) or telecodes.get(_pinyin(name))


def _row(raw: str, day: datetime):
    """One pipe-delimited train -> (code, depart, arrive), or None.

    Validated rather than trusted: this port reads positions in a string
    nobody promised to keep stable, so a row that does not look like a train
    is dropped here instead of becoming a confident wrong answer downstream.
    """
    parts = raw.split("|")
    if len(parts) <= max(FIELD.values()):
        return None
    code = parts[FIELD["code"]].strip().upper()
    if not re.fullmatch(r"[A-Z]\d{1,4}", code):
        return None
    try:
        stamp = datetime.strptime(parts[FIELD["date"]].strip(), "%Y%m%d").date()
        out = datetime.strptime(parts[FIELD["depart"]].strip(), "%H:%M").time()
        back = datetime.strptime(parts[FIELD["arrive"]].strip(), "%H:%M").time()
        hours, minutes = (int(x) for x in parts[FIELD["duration"]].split(":"))
    except (ValueError, IndexError):
        return None

    depart = datetime.combine(stamp, out, tzinfo=day.tzinfo)
    arrive = depart + timedelta(hours=hours, minutes=minutes)
    # The arrival clock is local and the duration is authoritative; an
    # overnight service arrives "before" it left if you trust the clock alone.
    if arrive.time() != back and arrive.date() == depart.date():
        arrive = datetime.combine(stamp, back, tzinfo=day.tzinfo)
        if arrive <= depart:
            arrive += timedelta(days=1)
    return code, depart, arrive


def connections(origin: str, destination: str, when: datetime,
                telecodes: dict[str, str] | None = None):
    """Trains still on sale between two stations on that date.

    ``origin`` and ``destination`` are station NAMES, as every other rail port
    in this engine takes them; the telecodes 12306 wants are resolved here.
    """
    telecodes = telecodes or stations()
    start, end = _telecode(telecodes, origin), _telecode(telecodes, destination)
    if not (start and end):
        missing = origin if not start else destination
        raise PortError(f"chinarail: 12306 lists no station called {missing!r}")

    key = {"from": start, "to": end, "on": f"{when:%Y-%m-%d}"}
    url = url_for(QUERY_URL, **{
        "leftTicketDTO.train_date": f"{when:%Y-%m-%d}",
        "leftTicketDTO.from_station": start,
        "leftTicketDTO.to_station": end,
        "purpose_codes": "ADULT"})
    # THE COOKIE. 12306 answers this endpoint only to a session it has issued,
    # so a bare GET returns an empty result set rather than an error -- which
    # would read here as "no trains today". `get_json` carries whatever the
    # caller's session provides; when that is nothing, an empty answer is
    # reported as a refusal below rather than published as a fact.
    return call("chinarail", key, lambda: get_json(url))


def offers(origin: str, destination: str, when: datetime, station_map: dict):
    """Wire format -> Offer. Same signature as every other rail port.

    ``station_map`` is the engine's name -> code table, so an Offer says where
    it went in the codes the rest of the system reasons about.
    """
    from offers import Offer

    codes = {_norm(name): code for name, code in (station_map or {}).items()}
    telecodes = stations()
    answered = connections(origin, destination, when, telecodes)
    rows = ((answered or {}).get("data") or {}).get("result") or []
    if not rows:
        raise PortError(
            "chinarail: 12306 returned no trains for that pair and date — "
            "an unissued session answers this way too, so it is reported "
            "rather than published as 'nothing runs'")

    out = []
    for raw in rows:
        parsed = _row(raw, when)
        if parsed is None:
            continue
        code, depart, arrive = parsed
        if code[0] not in TRAIN_TYPES:
            continue
        if depart < when or depart > when + HORIZON:
            continue
        out.append(Offer(
            id=f"cr-{code}-{depart:%Y%m%d%H%M}", mode="rail",
            carrier="China Railway",
            label=f"{code} {depart:%H:%M} - {origin} to {destination}",
            depart=depart, arrive=arrive,
            origin=codes.get(_norm(origin), ""),
            destination=codes.get(_norm(destination), ""),
            price=ESTIMATE, price_source="estimate", book_url=DEEP_LINK,
        ))
    return sorted(out, key=lambda o: o.depart)
