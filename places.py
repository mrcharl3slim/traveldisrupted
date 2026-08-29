"""City names to the codes and clocks the ports need.

A geocoder in a dict, and naming it as one is the point. Three things have to
be resolved before any provider can be called and none of them are in what a
traveller types: the airport code to search on, the country a hotel search
needs, and the zone a check-in time is local to. A real deployment resolves
these against reference data; a demo that pretends to have done so is worse
than one that shows its table, because the table is checkable and the pretence
is not.

Unknown places are not guessed. `find` returns None and the agent asks, which
is the honest failure and also the more useful one -- "I don't know that
airport, which one did you mean?" beats a confident search of the wrong city.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    code: str          # IATA, and what every search is keyed on
    label: str         # what a person calls it
    country: str       # ISO-2, which the hotel catalogue requires
    zone: str          # IANA, because a check-in time is local to the building
    city: str = ""     # the hotel search's name for it, when it differs

    @property
    def hotel_city(self) -> str:
        return self.city or self.label


PLACES: tuple[Place, ...] = (
    Place("SIN", "Singapore", "SG", "Asia/Singapore"),
    Place("LHR", "London", "GB", "Europe/London"),
    Place("ZRH", "Zurich", "CH", "Europe/Zurich"),
    Place("MXP", "Milan", "IT", "Europe/Rome"),
    Place("FLR", "Florence", "IT", "Europe/Rome"),
    Place("CDG", "Paris", "FR", "Europe/Paris"),
    Place("FCO", "Rome", "IT", "Europe/Rome"),
    Place("BCN", "Barcelona", "ES", "Europe/Madrid"),
    Place("MAD", "Madrid", "ES", "Europe/Madrid"),
    Place("AMS", "Amsterdam", "NL", "Europe/Amsterdam"),
    Place("FRA", "Frankfurt", "DE", "Europe/Berlin"),
    Place("MUC", "Munich", "DE", "Europe/Berlin"),
    Place("BKK", "Bangkok", "TH", "Asia/Bangkok"),
    Place("HKG", "Hong Kong", "HK", "Asia/Hong_Kong"),
    Place("NRT", "Tokyo", "JP", "Asia/Tokyo"),
    Place("ICN", "Seoul", "KR", "Asia/Seoul"),
    Place("TPE", "Taipei", "TW", "Asia/Taipei"),
    Place("KUL", "Kuala Lumpur", "MY", "Asia/Kuala_Lumpur"),
    Place("CGK", "Jakarta", "ID", "Asia/Jakarta"),
    Place("DPS", "Bali", "ID", "Asia/Makassar", city="Denpasar"),
    Place("SGN", "Ho Chi Minh City", "VN", "Asia/Ho_Chi_Minh"),
    Place("MNL", "Manila", "PH", "Asia/Manila"),
    Place("SYD", "Sydney", "AU", "Australia/Sydney"),
    Place("MEL", "Melbourne", "AU", "Australia/Melbourne"),
    Place("DXB", "Dubai", "AE", "Asia/Dubai"),
    Place("JFK", "New York", "US", "America/New_York"),
    Place("SFO", "San Francisco", "US", "America/Los_Angeles"),
    Place("BOM", "Mumbai", "IN", "Asia/Kolkata"),
    Place("DEL", "Delhi", "IN", "Asia/Kolkata"),
)

#: Spellings a traveller uses that are not the label. Kept separate from the
#: table so the table stays readable and an alias never quietly becomes a name.
ALIASES: dict[str, str] = {
    "sg": "SIN", "changi": "SIN",
    "london heathrow": "LHR", "heathrow": "LHR", "uk": "LHR",
    "milano": "MXP", "malpensa": "MXP",
    "firenze": "FLR",
    "zurich hb": "ZRH", "zurich airport": "ZRH",
    "roma": "FCO", "rome fiumicino": "FCO",
    "nyc": "JFK", "new york city": "JFK",
    "kl": "KUL", "denpasar": "DPS", "saigon": "SGN",
    "tokyo narita": "NRT", "hk": "HKG",
}

_BY_CODE = {p.code: p for p in PLACES}
_BY_NAME = {p.label.lower(): p for p in PLACES}
_BY_NAME.update({p.hotel_city.lower(): p for p in PLACES})


def find(text: str) -> Place | None:
    """A place, or None. Never a guess."""
    key = " ".join((text or "").strip().lower().split())
    if not key:
        return None
    if key.upper() in _BY_CODE:
        return _BY_CODE[key.upper()]
    if key in ALIASES:
        return _BY_CODE[ALIASES[key]]
    return _BY_NAME.get(key)


def by_code(code: str) -> Place | None:
    return _BY_CODE.get((code or "").strip().upper())


def label(code: str) -> str:
    place = by_code(code)
    return place.label if place else (code or "")


def names() -> list[str]:
    """Every spelling `find` accepts, longest first.

    Longest first matters: scanning a sentence for "york" before "new york"
    finds the wrong place, and the wrong place is a confident search of another
    continent.
    """
    out = {p.label.lower() for p in PLACES}
    out |= {p.hotel_city.lower() for p in PLACES}
    out |= set(ALIASES)
    out |= {p.code.lower() for p in PLACES}
    return sorted(out, key=len, reverse=True)
