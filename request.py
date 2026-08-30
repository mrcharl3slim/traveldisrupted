"""What the traveller asked for, and what is still missing.

    "i want to book a flight from singapore to london from 1 to 7 september"

ONE RULE DECIDES WHAT GETS ASKED. A question is worth asking only if the answer
changes what this system does. "Do you need a hotel" changes which providers
are called. "Cheapest, fastest, or direct" changes which offer comes back
first, and -- because the preference is stored with the trip -- which recovery
plan comes back first when something breaks a week later. "What's your seat
preference" changes nothing here, so it is not asked, however conversational it
would sound.

That rule is the difference between an assistant and a form with a chat window
in front of it. Every Ask below carries the reason it exists, and the reason is
shown to the traveller, because a question you can see the point of is a
question worth answering.

THE MODEL FILLS BLANKS AND NEVER OVERWRITES. Parsing runs deterministically
first -- cities, dates, party size, the words "direct" and "cheapest" -- and
whatever it resolves is final. The model is then asked only about what is still
empty, which is where it is genuinely better: "I need to be in London by
Tuesday" is language, not a pattern. The ordering means a model having an
imaginative day cannot move a date the traveller stated plainly, and the whole
thing still works with LLM_PROVIDER=none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import places

#: How a traveller wants the options ordered. Three, because these are the
#: three that a search can actually honour: they map to fare, duration and
#: stop count, all of which every offer already carries.
PREFERENCES = ("cheapest", "fastest", "direct")

MONTHS = {m: i + 1 for i, m in enumerate(
    ("january february march april may june july august september "
     "october november december").split())}
MONTHS.update({m[:3]: i for m, i in MONTHS.items()})
MONTHS["sept"] = 9


@dataclass(frozen=True)
class Ask:
    """One question, and the reason it is being asked.

    ``options`` are offered as chips rather than as a sentence: a traveller who
    can tap "cheapest" answers in a second, and the free-text box is still
    there for everyone whose answer is not on the list.
    """

    field: str
    question: str
    options: tuple[str, ...] = ()
    why: str = ""
    optional: bool = False


@dataclass(frozen=True)
class Request:
    kind: str = "book"                  # "book" | "confirmation"
    origin: str = ""                    # IATA, resolved
    destination: str = ""
    depart: date | None = None
    ret: date | None = None
    one_way: bool | None = None
    travellers: int = 1
    hotel: bool | None = None           # None = nobody has said yet
    hotel_area: str = ""
    #: The room's own nights. Asked rather than derived: arriving on the 18th
    #: does not mean checking in on the 18th -- a red-eye lands at 06:00 and
    #: the room is wanted from the night before, and a traveller staying with
    #: family for the first two nights wants three of the five. Deriving it
    #: from the flights is right often enough to be trusted and wrong quietly.
    hotel_in: date | None = None
    hotel_out: date | None = None
    preference: str = ""
    confirmed: bool = False
    #: Whether the summary card has actually been PUT IN FRONT of the
    #: traveller. Not the same as everything being answered, and the difference
    #: is a whole turn: the sentence that answers the last question also leaves
    #: `confirm` as the only gap, so deriving this from the gaps made the agent
    #: read that answer as a correction. "22 september", offered as the return
    #: date, was re-parsed as a fresh departure and overwrote the day out.
    shown: bool = False
    raw: str = ""
    filled: tuple[str, ...] = ()        # what the traveller settled, for the UI
    #: Which slots were filled from the traveller's profile rather than from
    #: this conversation. The card names them, because a slot filled without
    #: asking is safe only when the traveller can see that it was.
    from_profile: tuple[str, ...] = ()

    # -- what is still missing ------------------------------------------
    def gaps(self) -> list[Ask]:
        """The questions still worth asking, most blocking first."""
        out: list[Ask] = []
        if not self.destination:
            out.append(Ask("destination", "Where are you going?",
                           why="nothing can be searched without it"))
        if not self.origin:
            out.append(Ask("origin", "Where are you flying from?",
                           why="nothing can be searched without it"))
        if self.depart is None:
            out.append(Ask("depart", "Which day do you want to leave?",
                           why="fares and availability are per-date"))
        if self.depart is not None and self.ret is None and self.one_way is None:
            out.append(Ask("ret", "Coming back, or one way?",
                           options=("coming back", "one way"),
                           why="a return leg is a second search and a second "
                               "contract — and two contracts are why nobody "
                               "owes you the connection"))
        if self.depart is not None and self.ret is None and self.one_way is False:
            # A different question, because the first one has been answered.
            # "Coming back, or one way?" asks two things -- whether there is a
            # return and when it is -- and "coming back" settles only the
            # first. Re-asking the original is how an agent gets stuck in a
            # loop with somebody who has already told it the answer.
            out.append(Ask("ret_date", "Which day are you coming back?",
                           why="the return is its own search on its own date"))
        if self.hotel is None:
            out.append(Ask("hotel", "Do you want a hotel as well?",
                           options=("yes", "no"),
                           why="it decides whether a stay is searched at all — "
                               "and a room is usually the thing a delayed "
                               "flight actually costs you"))
        if self.hotel and not (self.hotel_in and self.hotel_out):
            # Immediately after "do you want a hotel", because it is the same
            # subject. The whole trip is offered as a chip so the common answer
            # is one tap, and typing a different range is still one sentence.
            span = self.trip_span
            out.append(Ask("hotel_dates", "Which nights do you need the room?",
                           options=((f"{span[0]:%d %b} – {span[1]:%d %b} "
                                     "(the whole trip)",) if span else ()),
                           why="a hotel is the largest number on most "
                               "itineraries and the easiest to get quietly "
                               "wrong — a red-eye lands at 06:00 and the room "
                               "is wanted from the night before"))
        if not self.preference:
            out.append(Ask("preference", "What matters most on this trip?",
                           options=PREFERENCES,
                           why="it orders the options now, and it orders the "
                               "recovery plans if this trip goes wrong later"))
        if self.hotel and not self.hotel_area:
            out.append(Ask("hotel_area", "Anywhere in particular to stay?",
                           options=("anywhere",),
                           why="only narrows the search; skip it and you get "
                               "the whole city",
                           optional=True))
        if not [a for a in out if not a.optional] and not self.confirmed:
            # The last gate before anything is searched. Dates and airports are
            # the two things a sentence gets wrong most often and the two the
            # traveller can check in a second -- and every number after this
            # point is computed against them.
            out.append(Ask("confirm", "Have I got this right?",
                           options=("yes, search it", "no, let me change it"),
                           why="every fare and every deadline after this is "
                               "worked out from what is written here"))
        # Blocking questions first. An optional one listed ahead of the
        # confirmation makes any caller that reads asks[0] show the wrong
        # thing -- and "anywhere in particular to stay?" is a strange chip to
        # offer under "have I got this right?".
        return sorted(out, key=lambda a: a.optional)

    @property
    def ready(self) -> bool:
        """Enough to search. Optional gaps do not hold anything up."""
        return not [a for a in self.gaps() if not a.optional]

    def card(self) -> list[dict]:
        """What the system has recorded, laid out to be checked line by line."""
        def mark(name: str, note: str) -> str:
            if name not in self.from_profile:
                return note
            return f"{note} · from your profile" if note else "from your profile"
        out = [
            {"label": "From", "value": places.label(self.origin) or "—",
             "note": mark("origin", self.origin)},
            {"label": "To", "value": places.label(self.destination) or "—",
             "note": self.destination},
            {"label": "Out", "value": (f"{self.depart:%A %d %B}"
                                       if self.depart else "—")},
        ]
        out.append({"label": "Back",
                    "value": (f"{self.ret:%A %d %B}" if self.ret
                              else "one way" if self.one_way else "—")})
        if self.travellers > 1:
            out.append({"label": "Travellers", "value": str(self.travellers)})
        if self.hotel and self.check_in and self.check_out:
            out.append({"label": "Hotel",
                        "value": (f"{self.check_in:%d %b} – {self.check_out:%d %b}"
                                  f" · {self.nights} night"
                                  + ("s" if self.nights != 1 else "")),
                        "note": places.label(self.destination)})
        else:
            out.append({"label": "Hotel", "value": "not needed"})
        out.append({"label": "Sort by", "value": self.preference or "cheapest",
                    "note": mark("preference", "")})
        return out

    @property
    def settled(self) -> tuple:
        """Only the parts a traveller can answer.

        `raw` and `filled` are bookkeeping -- `raw` changes on every turn
        because it holds whatever was last typed. Comparing whole Requests to
        ask "did that message tell me anything?" answers yes to "banana", which
        is how an unreadable reply slips past the acknowledgement and the
        question repeats with a straight face.
        """
        return (self.origin, self.destination, self.depart, self.ret,
                self.one_way, self.travellers, self.hotel, self.hotel_area,
                self.hotel_in, self.hotel_out, self.preference, self.confirmed)

    @property
    def trip_span(self) -> tuple | None:
        """The nights the trip itself covers, when they are known."""
        from datetime import timedelta as _td

        if not self.depart:
            return None
        return self.depart, (self.ret or self.depart + _td(days=1))

    @property
    def nights(self) -> int:
        if self.hotel_in and self.hotel_out:
            return max(1, (self.hotel_out - self.hotel_in).days)
        if self.depart and self.ret:
            return max(1, (self.ret - self.depart).days)
        return 1

    @property
    def check_in(self):
        return self.hotel_in or self.depart

    @property
    def check_out(self):
        from datetime import timedelta as _td

        if self.hotel_out:
            return self.hotel_out
        if not self.depart:
            return None
        return self.ret or (self.depart + _td(days=self.nights))

    def summary(self) -> str:
        where = f"{places.label(self.origin)} to {places.label(self.destination)}"
        when = f"{self.depart:%d %b}" if self.depart else "date to confirm"
        if self.ret:
            when += f" – {self.ret:%d %b}"
        elif self.one_way:
            when += ", one way"
        bits = [where, when]
        if self.travellers > 1:
            bits.append(f"{self.travellers} travellers")
        if self.hotel:
            bits.append(f"hotel{' in ' + self.hotel_area if self.hotel_area else ''}")
        if self.preference:
            bits.append(self.preference)
        return " · ".join(bits)


# --------------------------------------------------------------------------
# deterministic parsing
# --------------------------------------------------------------------------

_DATE_RANGE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:to|-|–|until|till)\s*"
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]+)", re.I)
_DATE_ONE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]+)(?!\w)", re.I)
_DATE_MONTH_FIRST = re.compile(
    r"([a-z]+)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th)?)?(?:\s*(?:to|-|–)\s*(\d{1,2}))?",
    re.I)
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_PEOPLE = re.compile(r"(\d+)\s*(?:adults?|people|persons?|pax|travell?ers?)", re.I)

#: Saying no to "have I got this right?" is an ANSWER. It settles nothing, so
#: the slot stays open -- but it is understood, and treating it as gibberish
#: produced the worst line in the product: tapping "no, let me change it" and
#: being told "Sorry, I couldn't make that out."
DECLINE = re.compile(r"\s*(n|no|nope|nah|not quite|wrong|change|fix|edit|"
                     r"let me)\b", re.I)


def declined(value: str) -> bool:
    return bool(DECLINE.match(value or ""))


#: Days with no month, which is how a person corrects a date they are looking
#: at: "actually the 19th to the 22nd". Only ordinals or a leading "the" count
#: -- a bare "2" is far more likely to be two adults or two nights, and reading
#: it as a date would be the confident kind of wrong.
_BARE_RANGE = re.compile(
    r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\s*(?:to|-|–|until|till)\s*"
    r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)", re.I)
_BARE_ONE = re.compile(r"(?:the\s+)(\d{1,2})(?:st|nd|rd|th)?(?!\d)", re.I)


def _resolve_year(month: int, day: int, today: date) -> date:
    """A month with no year means the next one that has not happened.

    "1 September" said in October is next September, not one eleven months
    gone. Guessing backwards produces a search for a date in the past, which
    every provider answers with silence.
    """
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return today
        if candidate >= today:
            return candidate
    return date(today.year + 1, month, day)


def _bare(text: str, context: date) -> tuple[date | None, date | None]:
    """Days read against a month the traveller is already looking at."""
    def on(day: int) -> date | None:
        try:
            return date(context.year, context.month, day)
        except ValueError:
            return None

    m = _BARE_RANGE.search(text)
    if m:
        start, end = on(int(m.group(1))), on(int(m.group(2)))
        return start, (end if start and end and end >= start else None)
    m = _BARE_ONE.search(text)
    return (on(int(m.group(1))), None) if m else (None, None)


def _after(day: date, floor: date) -> date:
    """The first time this day-of-month falls on or after ``floor``.

    A day with no month, answered to "which day are you coming back?", belongs
    to the month the traveller is travelling in -- unless that has already
    passed, in which case it belongs to the next one. A return before the
    departure is not a date this engine should be willing to store.
    """
    year, month = floor.year, floor.month
    for _ in range(13):
        try:
            candidate = date(year, month, day.day)
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= floor:
            return candidate
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return floor


def _dates(text: str, today: date,
           context: date | None = None) -> tuple[date | None, date | None]:
    iso = _ISO.findall(text)
    if iso:
        found = [date(int(y), int(m), int(d)) for y, m, d in iso]
        return found[0], (found[1] if len(found) > 1 else None)

    m = _DATE_RANGE.search(text)
    if m and m.group(3).lower() in MONTHS:
        month = MONTHS[m.group(3).lower()]
        start = _resolve_year(month, int(m.group(1)), today)
        end = _resolve_year(month, int(m.group(2)), today)
        # A range that ends before it starts crossed a month boundary.
        return start, (end if end >= start else None)

    m = _DATE_MONTH_FIRST.search(text)
    if m and m.group(1).lower() in MONTHS:
        month = MONTHS[m.group(1).lower()]
        start = _resolve_year(month, int(m.group(2)), today)
        end = (_resolve_year(month, int(m.group(3)), today)
               if m.group(3) else None)
        return start, (end if end and end >= start else None)

    m = _DATE_ONE.search(text)
    if m and m.group(2).lower() in MONTHS:
        return _resolve_year(MONTHS[m.group(2).lower()], int(m.group(1)), today), None
    # Nothing named a month. If the traveller is correcting a date they can
    # see, the month is the one already on the card.
    return _bare(text, context) if context else (None, None)


def _endpoints(text: str) -> tuple[str, str]:
    """Origin and destination, by scanning for known places in order.

    "from X to Y" when it is there, and otherwise the first two places
    mentioned in the order they were mentioned -- "singapore london next week"
    is a sentence people type.
    """
    low = f" {text.lower()} "
    hits: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for name in places.names():                # longest first
        for m in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            if any(s <= m.start() < e for s, e in taken):
                continue
            found = places.find(name)
            if found:
                hits.append((m.start(), found.code))
                taken.append((m.start(), m.end()))
    hits.sort()
    codes: list[str] = []
    for _pos, code in hits:
        if code not in codes:
            codes.append(code)

    from_to = re.search(r"from\s+(.+?)\s+to\s+([a-z\s]+?)(?:\s+(?:from|on|in|"
                        r"between|departing|leaving|next|this)\b|[,.]|$)", low)
    if from_to:
        a, b = places.find(from_to.group(1)), places.find(from_to.group(2))
        if a and b:
            return a.code, b.code
    if len(codes) >= 2:
        return codes[0], codes[1]
    if len(codes) == 1:
        # One place named after "to" is a destination; otherwise assume origin.
        only = codes[0]
        label = places.by_code(only)
        for name in (label.label.lower(), label.hotel_city.lower(), only.lower()):
            if re.search(rf"to\s+{re.escape(name)}", low):
                return "", only
        return only, ""
    return "", ""


def parse(text: str, today: date | None = None,
          context: date | None = None) -> Request:
    """Free text -> whatever can be established without guessing.

    ``context`` is a date already on screen, used only to give a bare "the
    19th" a month. Never used to invent a date out of nothing.
    """
    today = today or date.today()
    low = (text or "").lower()
    origin, destination = _endpoints(text)
    depart, ret = _dates(low, today, context)

    hotel: bool | None = None
    if re.search(r"\b(no|without|don'?t need a?)\s+(hotel|room|accommodation)", low):
        hotel = False
    elif re.search(r"\b(hotel|room|stay|accommodation|place to sleep)\b", low):
        hotel = True

    preference = ""
    if re.search(r"\b(direct|non-?stop|no stops?)\b", low):
        preference = "direct"
    elif re.search(r"\b(fastest|quickest|shortest|soonest)\b", low):
        preference = "fastest"
    elif re.search(r"\b(cheap(est)?|budget|lowest fare|best price)\b", low):
        preference = "cheapest"

    nights = re.search(r"(\d+)\s*nights?\b", low)
    people = _PEOPLE.search(low)
    stay_in = stay_out = None
    if nights and depart:
        stay_in = depart
        stay_out = depart + timedelta(days=max(1, min(int(nights.group(1)), 60)))
    travellers = int(people.group(1)) if people else 1

    # "one way" said out loud beats a second date found in the sentence. A
    # traveller who writes "london 3-9 nov one way" has named the trip's shape
    # and a range of days to travel in, not a return -- and reading it as a
    # return silently books a leg nobody asked for.
    if re.search(r"\bone[-\s]?way\b", low):
        one_way, ret = True, None
    elif ret is not None:
        one_way = False
    elif re.search(r"\b(coming back|come back|returning|round[-\s]?trip|"
                   r"two[-\s]?way|both ways|and back)\b", low):
        # Said in the opening sentence and previously ignored, so the agent
        # asked "coming back, or one way?" to somebody who had just answered
        # it. A question you have already been told the answer to is the
        # fastest way to look like a form.
        one_way = False
    else:
        one_way = None

    return Request(
        kind="book",
        origin=origin, destination=destination,
        depart=depart, ret=ret, one_way=one_way,
        travellers=max(1, min(travellers, 9)),
        hotel=hotel, preference=preference, raw=text,
        hotel_in=stay_in, hotel_out=stay_out,
    )


# --------------------------------------------------------------------------
# the model, on the blanks only
# --------------------------------------------------------------------------

#: Anything that could possibly refer to a day, and anything that could
#: possibly refer to somewhere to sleep. Deliberately loose: neither is trying
#: to read the value, only to answer "did the traveller bring this up at all?"
#: See `mentions_a_day`.
_DAY_CUE = re.compile(
    r"\d"                                    # 18 september, the 3rd, 9/18
    r"|\b(?:" + "|".join(sorted(MONTHS)) + r")\b"
    r"|\b(?:today|tonight|tomorrow|yesterday|overnight|weekend|new year"
    r"|mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:next|this|coming|following|last|end\s+of|start\s+of)\s+\w+"
    r"|\bin\s+(?:a|an|one|two|three|four|five|six|seven)\b"
    r"|\bday\s+after\b|\bday\s+before\b", re.I)
_STAY_CUE = re.compile(
    r"\b(?:hotel|hotels|room|rooms|stay|stays|staying|night|nights|bed|beds"
    r"|accommodation|lodging|hostel|airbnb|apartment|apartments|b&b|b\s?and\s?b"
    r"|somewhere\s+to\s+(?:stay|sleep)|place\s+to\s+(?:stay|sleep))\b", re.I)


def mentions_a_day(text: str) -> bool:
    """Did the traveller refer to a day at all?

    The model is asked for blanks only, which fences it from overwriting
    anything stated. That fence has a gap: it stops the model MOVING a value
    and does nothing about the model SUPPLYING one out of nothing, and every
    blank is by definition unfenced.

    The prompt names today so relative phrasing can be resolved, which also
    hands the model a date to reach for when the text has none. Asked to read
    the two words "one way", Haiku returns today as the departure -- every
    other key correctly null. It is not being careless: a flight leaves on a
    day, the only day in front of it is the one in the prompt, and "Do not
    infer beyond what is written" loses to that.

    So a supplied date is corroborated instead: the sentence has to contain
    something that refers to a day before one is taken from it. This is not
    parsing -- "next Thursday" is a cue and not a date, and reading it is
    exactly what the model is here for. It only has to be a cue the traveller
    actually put there.
    """
    return bool(_DAY_CUE.search(text or ""))


def mentions_a_stay(text: str) -> bool:
    """The same question about a room, and the more expensive one to get wrong.

    A hallucinated date is at least visible -- it goes on the card, in full,
    for the traveller to check. `hotel` is a boolean, and the wrong one is
    invisible: `false` does not appear on the card, it DELETES the question,
    and nothing downstream ever asks again. "One way" came back with
    `hotel: false` attached and the room was never mentioned again -- which is
    the one thing `converse` says it will never do, decided by a model that had
    been shown two words about a return leg.
    """
    return bool(_STAY_CUE.search(text or ""))


_PROMPT = """Read this travel request and return ONLY JSON.

Request: {text}
Today: {today}

Return these keys, using null where the request does not say:
  origin_city, destination_city  (city names, not codes)
  depart, ret                    (YYYY-MM-DD)
  travellers                     (integer)
  hotel                          (true/false)
  preference                     ("cheapest", "fastest" or "direct")

Do not infer beyond what is written. "Next Tuesday" is a date; "somewhere warm"
is not a destination.
"""


def enrich(base: Request, model, today: date | None = None) -> Request:
    """Let the model fill what deterministic parsing could not.

    Blanks only, and never a correction. A traveller who wrote "1 September"
    has said what they mean, and a model that reads it as the 9th of January
    must not be able to act on that -- so anything already resolved is passed
    over here rather than compared. What is left is the phrasing a pattern
    cannot reach, which is the only place the model was going to help.
    """
    if model is None or base.ready:
        return base
    today = today or date.today()
    try:
        reply = model.invoke(_PROMPT.format(text=base.raw, today=today.isoformat()))
        body = getattr(reply, "content", reply)
        if isinstance(body, list):                # some providers chunk it
            body = "".join(p.get("text", "") for p in body if isinstance(p, dict))
        data = json.loads(re.search(r"\{.*\}", str(body), re.S).group(0))
    except Exception as exc:                      # noqa: BLE001
        # Arithmetic still works, and that is the point -- but a swallowed
        # failure that nobody records is how a broken token looks identical to
        # a working one for a whole demo.
        import _model

        _model.note_failure(exc)
        return base

    def place_code(value) -> str:
        found = places.find(str(value or ""))
        return found.code if found else ""

    def when(value) -> date | None:
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    changes: dict = {}
    if not base.origin and place_code(data.get("origin_city")):
        changes["origin"] = place_code(data["origin_city"])
    if not base.destination and place_code(data.get("destination_city")):
        changes["destination"] = place_code(data["destination_city"])
    stated_a_day = mentions_a_day(base.raw)
    if base.depart is None and when(data.get("depart")) and stated_a_day:
        changes["depart"] = when(data["depart"])
    # Not when they have already said one way. A return date is then a
    # contradiction rather than a blank, and `one_way` was being flipped back
    # to False as a side effect of accepting it -- so "one way" followed by any
    # sentence with a number in it quietly grew a return leg.
    if base.ret is None and not base.one_way and stated_a_day:
        ret = when(data.get("ret"))
        depart = changes.get("depart", base.depart)
        # A return before the outbound is not a return. It is the anchor date
        # coming back with a couple of nights added to it.
        if ret and not (depart and ret < depart):
            changes["ret"] = ret
            if base.one_way is None:
                changes["one_way"] = False
    if (base.hotel is None and isinstance(data.get("hotel"), bool)
            and mentions_a_stay(base.raw)):
        changes["hotel"] = data["hotel"]
    if not base.preference and str(data.get("preference", "")).lower() in PREFERENCES:
        changes["preference"] = str(data["preference"]).lower()
    if base.travellers == 1 and isinstance(data.get("travellers"), int):
        changes["travellers"] = max(1, min(data["travellers"], 9))
    return replace(base, **changes) if changes else base


# --------------------------------------------------------------------------
# answers coming back
# --------------------------------------------------------------------------

def answer(base: Request, field_name: str, value: str,
           today: date | None = None) -> Request:
    """Apply one answer to one slot. Unreadable answers leave the slot open.

    Leaving it open is deliberate: the agent asks again rather than storing
    something it could not read, because a silently dropped answer is a
    question the traveller believes they have already dealt with.
    """
    today = today or date.today()
    value = (value or "").strip()
    filled = tuple(dict.fromkeys(base.filled + (field_name,)))

    if field_name in ("origin", "destination"):
        found = places.find(value)
        if not found:
            return base
        other = "destination" if field_name == "origin" else "origin"
        if getattr(base, other) == found.code:
            # The same place cannot be both ends. One name in a sentence with
            # no "to" or "from" is genuinely ambiguous -- "london 3-9 nov" --
            # so it was filed as an origin and the traveller has just said it
            # is the destination. Move it rather than producing LHR to LHR.
            return replace(base, filled=filled,
                           **{field_name: found.code, other: ""})
        return replace(base, filled=filled, **{field_name: found.code})
    if field_name == "depart":
        found, _ = _dates(value.lower(), today, base.depart)
        return replace(base, depart=found, filled=filled) if found else base
    if field_name in ("ret", "ret_date"):
        # Three outcomes, not two. A date settles it; "one way" settles it the
        # other way; and "coming back" / "two way" / "yes" settles WHETHER
        # there is a return without saying when -- which is a real answer and
        # has to be recorded, or the next turn asks the same question again.
        if field_name == "ret" and re.search(
                r"\bone[-\s]?way\b|^no\b|^nope\b", value, re.I):
            return replace(base, one_way=True, ret=None, filled=filled)
        found, second = _dates(value.lower(), today)
        found = second or found
        if found is None and base.depart is not None:
            # "the 22nd". Asking the question named the month, so a bare day is
            # a complete answer here even though it is not one in an opening
            # sentence, where a lone number is more likely to be two adults.
            bare_start, bare_end = _bare(value.lower(), base.depart)
            found = bare_end or bare_start
            if found is not None and found < base.depart:
                found = _after(found, base.depart)
        if found:
            return replace(base, ret=found, one_way=False, filled=filled)
        if re.search(r"\b(coming back|come back|return(ing)?|round[-\s]?trip|"
                     r"two[-\s]?way|both ways|yes|yeah|yep)\b", value, re.I):
            return replace(base, one_way=False, ret=None, filled=filled)
        return base
    if field_name == "hotel":
        # Yes, no, and neither -- three outcomes, because a yes/no slot that
        # treats everything-that-is-not-yes as no is worse than one that asks
        # again. "banana" became "no hotel" and the traveller was never told:
        # a decision they did not make, stored silently, which is the exact
        # failure this whole product exists to catch other people committing.
        if re.match(r"\s*(y|yes|yeah|yep|sure|please|ok|okay|definitely)\b",
                    value, re.I) or re.search(r"\b(hotel|room|stay|"
                                              r"accommodation)\b", value, re.I):
            return replace(base, hotel=True, filled=filled)
        if re.match(r"\s*(n|no|nope|nah|don'?t|not?\b)", value, re.I):
            return replace(base, hotel=False, filled=filled)
        return base
    if field_name == "preference":
        low = value.lower()
        for option in PREFERENCES:
            if option in low:
                return replace(base, preference=option, filled=filled)
        if re.search(r"non-?stop|no stops?", low):
            return replace(base, preference="direct", filled=filled)
        return base
    if field_name == "hotel_area":
        if re.match(r"any|no|whatever|don'?t mind", value, re.I):
            return replace(base, hotel_area="anywhere", filled=filled)
        return replace(base, hotel_area=value[:60], filled=filled)
    if field_name == "hotel_dates":
        span = base.trip_span
        if span and re.search(r"whole trip|same|all of it|entire|yes", value, re.I):
            return replace(base, hotel_in=span[0], hotel_out=span[1], filled=filled)
        start, end = _dates(value.lower(), today, base.depart)
        if start and end:
            return replace(base, hotel_in=start, hotel_out=end, filled=filled)
        nights = re.search(r"(\d+)\s*nights?", value, re.I)
        if nights and base.depart:
            from datetime import timedelta as _td

            count = max(1, min(int(nights.group(1)), 60))
            return replace(base, hotel_in=base.depart,
                           hotel_out=base.depart + _td(days=count), filled=filled)
        # A single date is half an answer, and half an answer stored is a
        # checkout somebody never chose.
        return base
    if field_name == "confirm":
        if re.match(r"\s*(y|yes|yeah|yep|correct|right|ok|okay|search|go)",
                    value, re.I):
            return replace(base, confirmed=True, filled=filled)
        return base                       # anything else is a correction
    if field_name == "travellers":
        digits = re.search(r"\d+", value)
        return (replace(base, travellers=max(1, min(int(digits.group()), 9)),
                        filled=filled) if digits else base)
    return base
