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
    #: The main station, as the timetable spells it, and a code for the engine.
    #: Two fields because they answer different questions: `station` is what
    #: transport.opendata.ch is asked, and `station_code` is what the engine
    #: reasons about -- a station is not its airport, and a trip that lands at
    #: Malpensa and leaves from Milano Centrale has a fifty-minute problem the
    #: engine can only see if the two places have different names.
    #:
    #: Empty where there is no station worth naming, and that is a real answer
    #: rather than a gap: `flow.search_rail` returns nothing for a place with
    #: no station, exactly as it returns nothing for a destination no airline
    #: serves. Naming a station does NOT claim the timetable can route to it --
    #: that is the API's answer, and an unroutable pair comes back empty.
    station: str = ""
    station_code: str = ""

    @property
    def hotel_city(self) -> str:
        return self.city or self.label


PLACES: tuple[Place, ...] = (
    Place("SIN", "Singapore", "SG", "Asia/Singapore"),
    Place("LHR", "London", "GB", "Europe/London",
          station="London St Pancras", station_code="LONDON_STP"),
    Place("ZRH", "Zurich", "CH", "Europe/Zurich",
          station="Zurich HB", station_code="ZRH_HB"),
    Place("MXP", "Milan", "IT", "Europe/Rome",
          station="Milano Centrale", station_code="MILANO_C"),
    Place("FLR", "Florence", "IT", "Europe/Rome",
          station="Firenze S.M.N.", station_code="FIRENZE_SMN"),
    Place("CDG", "Paris", "FR", "Europe/Paris",
          station="Paris Gare de Lyon", station_code="PARIS_GDL"),
    Place("FCO", "Rome", "IT", "Europe/Rome",
          station="Roma Termini", station_code="ROMA_TERMINI"),
    Place("BCN", "Barcelona", "ES", "Europe/Madrid",
          station="Barcelona Sants", station_code="BARCELONA_SANTS"),
    Place("MAD", "Madrid", "ES", "Europe/Madrid",
          station="Madrid Puerta de Atocha", station_code="MADRID_ATOCHA"),
    Place("AMS", "Amsterdam", "NL", "Europe/Amsterdam",
          station="Amsterdam Centraal", station_code="AMSTERDAM_CS"),
    Place("FRA", "Frankfurt", "DE", "Europe/Berlin",
          station="Frankfurt (Main) Hbf", station_code="FRANKFURT_HBF"),
    Place("MUC", "Munich", "DE", "Europe/Berlin",
          station="Munchen Hbf", station_code="MUENCHEN_HBF"),
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

    # Mainland China. One timezone for the whole country -- Asia/Shanghai
    # everywhere, Urumqi included, whatever the sun is doing there -- so these
    # need no per-city research and cannot be wrong about the clock in the way
    # a guessed zone would be.
    #
    # FLIGHTS ONLY, DELIBERATELY. A row here makes a place searchable by air
    # and nothing more: the only rail API this reaches is the Swiss timetable,
    # which does not route China and has never claimed to. Giving these rows a
    # station would be a promise `search_rail` cannot keep -- it would return
    # empty and read as a fault rather than as an absence. See the note under
    # the table.
    Place("PVG", "Shanghai", "CN", "Asia/Shanghai",
          station="Shanghai Hongqiao", station_code="SHANGHAI_HQ"),
    Place("PEK", "Beijing", "CN", "Asia/Shanghai",
          station="Beijing South", station_code="BEIJING_S"),
    Place("CAN", "Guangzhou", "CN", "Asia/Shanghai",
          station="Guangzhou South", station_code="GUANGZHOU_S"),
    Place("SZX", "Shenzhen", "CN", "Asia/Shanghai",
          station="Shenzhen North", station_code="SHENZHEN_N"),
    Place("CTU", "Chengdu", "CN", "Asia/Shanghai",
          station="Chengdu East", station_code="CHENGDU_E"),
    Place("XIY", "Xi'an", "CN", "Asia/Shanghai",
          station="Xi'an North", station_code="XIAN_N"),
    Place("KMG", "Kunming", "CN", "Asia/Shanghai"),
    Place("HGH", "Hangzhou", "CN", "Asia/Shanghai",
          station="Hangzhou East", station_code="HANGZHOU_E"),
    Place("NKG", "Nanjing", "CN", "Asia/Shanghai",
          station="Nanjing South", station_code="NANJING_S"),
    Place("WUH", "Wuhan", "CN", "Asia/Shanghai",
          station="Wuhan", station_code="WUHAN"),
    Place("CKG", "Chongqing", "CN", "Asia/Shanghai",
          station="Chongqing West", station_code="CHONGQING_W"),
    Place("TSN", "Tianjin", "CN", "Asia/Shanghai"),
    Place("XMN", "Xiamen", "CN", "Asia/Shanghai",
          station="Xiamen North", station_code="XIAMEN_N"),
    Place("CSX", "Changsha", "CN", "Asia/Shanghai",
          station="Changsha South", station_code="CHANGSHA_S"),
    Place("TAO", "Qingdao", "CN", "Asia/Shanghai"),
    Place("DLC", "Dalian", "CN", "Asia/Shanghai"),
    Place("SHE", "Shenyang", "CN", "Asia/Shanghai"),
    Place("HRB", "Harbin", "CN", "Asia/Shanghai"),
    Place("URC", "Urumqi", "CN", "Asia/Shanghai"),
    Place("SYX", "Sanya", "CN", "Asia/Shanghai"),
    Place("HAK", "Haikou", "CN", "Asia/Shanghai"),
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
    # China: the second airport a city is known by, and the spellings a
    # traveller reaches for. PKX and SHA are real airports of their own; they
    # resolve to the city because the engine reasons about cities, and picking
    # the terminal is the airline's job in the search that follows.
    "hongkong": "HKG", "hong-kong": "HKG",
    "shanghai": "PVG", "sha": "PVG", "pudong": "PVG", "hongqiao": "PVG",
    "beijing": "PEK", "peking": "PEK", "pkx": "PEK", "daxing": "PEK",
    "guangzhou": "CAN", "canton": "CAN",
    "shenzhen": "SZX", "chengdu": "CTU", "tfu": "CTU",
    "xian": "XIY", "xi an": "XIY",
    "kunming": "KMG", "hangzhou": "HGH", "nanjing": "NKG", "wuhan": "WUH",
    "chongqing": "CKG", "tianjin": "TSN", "xiamen": "XMN", "changsha": "CSX",
    "qingdao": "TAO", "dalian": "DLC", "shenyang": "SHE", "harbin": "HRB",
    "urumqi": "URC", "sanya": "SYX", "haikou": "HAK",
}

#: The Swiss timetable is the only rail API this reaches, so the table above is
#: where rail coverage begins and the API is where it ends. `station` names a
#: platform; whether a train runs between two of them is not ours to assert.
_BY_STATION = {p.station_code: p for p in PLACES if p.station_code}

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
    """The place a code names -- an airport code or a station code.

    Both, because a rail leg puts a station code where every other part of the
    engine expects an airport one, and a lookup that answered None for
    MILANO_C would strand every consequence of a train.
    """
    key = (code or "").strip().upper()
    return _BY_CODE.get(key) or _BY_STATION.get(key)


def station_map() -> dict[str, str]:
    """Station name -> the code the engine reasons about.

    The timetable answers in its own spellings, so the port needs this to say
    where a train actually went. Names it does not know fall through to the
    port's own default rather than being invented here.
    """
    return {p.station: p.station_code for p in PLACES if p.station}


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
