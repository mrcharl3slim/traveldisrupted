"""Things the traveller has to be at, which nobody sold them.

    "meeting with the Milan team on 19 September at 10am at their office"

WHY THIS IS A BOOKING AND NOT A NEW KIND OF THING. An appointment is a place, a
time and a consequence for not being there -- which is exactly what every other
row in an itinerary is. Modelling it as its own concept would mean a second
reachability check, a second clash rule and a second set of recovery plans, and
the second one is always the one that rots. So `to_booking` turns it into a
`Booking` with no price and `commitment=True`, and from that moment the whole
engine already knows what to do with it: `propagate` marks it broken when the
traveller cannot get there, `plan.generate` weighs recovery options that save
it, and the deadline watch counts down to it.

The `commitment` flag is the only thing that had to be added, and it exists
because the engine reads value from `price`. A EUR 0 dinner with free
cancellation genuinely costs nothing to miss. A EUR 0 meeting with the Milan
team is the reason the trip exists. Without the flag the second is filed under
"missed, but nothing prepaid", which is the most expensive wrong answer this
system could give.

WHAT IS NEVER GUESSED. Who, when and where. A meeting whose time we invented is
worse than no meeting in the system at all, because it will be checked against
flights and pronounced feasible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

import places
from domain import Booking, Kind
from request import MONTHS, Ask, _dates

#: What a meeting takes if nobody says. An hour is the commonest working
#: default and it is a *stated* assumption -- shown on screen, and overridden
#: by anyone who says "two hours".
DEFAULT_MINUTES = 60

#: Getting from where you land to where a meeting is. Coarse on purpose: the
#: engine already knows airport-to-city transit, and this covers the last hop
#: to an address it has never heard of. Being roughly right and visible beats
#: being precisely wrong and silent.
LOCAL_HOP = timedelta(minutes=45)

_TIME = re.compile(
    r"(?<!\d)(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)"
    r"|(?<!\d)(\d{1,2}):(\d{2})(?!\d)", re.I)
_DURATION = re.compile(
    r"(?:for\s+)?(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
_WITH = re.compile(
    r"\bwith\s+(?:the\s+)?([A-Za-z][\w'&.\- ]{1,48}?)"
    r"(?=\s+(?:on|at|in|from|next|this|tomorrow|for)\b|[,.]|$)", re.I)
_AT_PLACE = re.compile(
    r"\b(?:at|in)\s+(?:the\s+)?([A-Za-z][\w'&.\- ]{1,48}?)"
    r"(?=\s+(?:on|at|from|next|this|tomorrow|for|with)\b|[,.]|$)", re.I)


@dataclass(frozen=True)
class Appointment:
    what: str = ""
    who: str = ""
    where: str = ""            # what the traveller called it
    place: str = ""            # resolved code, when it is a place we know
    when: datetime | None = None
    day: date | None = None    # known day, unknown time
    minutes: int = DEFAULT_MINUTES
    trip_id: str = ""
    confirmed: bool = False
    raw: str = ""

    def gaps(self) -> list[Ask]:
        """Who, when and where -- and nothing else.

        Three questions because three things decide whether this is possible:
        the day and time place it against the itinerary, the location decides
        whether the traveller can physically get there, and who it is with is
        what makes the answer mean anything when it is read back.
        """
        out: list[Ask] = []
        if not self.who:
            out.append(Ask("who", "Who is this with?",
                           why="so the clash reads as something you recognise "
                               "rather than a slot in a calendar"))
        if self.day is None:
            out.append(Ask("day", "Which day is it?",
                           why="it decides which of your trips this belongs to"))
        elif self.when is None:
            out.append(Ask("at", "What time?",
                           options=("morning", "afternoon"),
                           why="a flight that lands at 14:00 clears a 16:00 "
                               "meeting and misses a 10:00 one"))
        if not self.where:
            out.append(Ask("where", "Where is it?",
                           why="whether you can get there from where you land "
                               "is the whole question"))
        elif not self.place:
            # The location is not optional colour -- it decides the CLOCK.
            # "9am at the Ritz Carlton" means nine in the morning in New York,
            # and until the engine knows which city that is it has to guess a
            # timezone. It guessed wrong once already: a 09:00 meeting stamped
            # in the departure city's zone landed twelve hours out and clashed
            # with a flight the traveller had already got off.
            out.append(Ask("place", f"Which city is {self.where} in?",
                           why="the time depends on it — 9am at the Ritz "
                               "Carlton means 9am in New York, and I have to "
                               "know which city that is before I can check "
                               "anything against your flights"))
        if not out and not self.confirmed:
            out.append(Ask("confirm", "Have I got this right?",
                           options=("yes, save it", "no, let me change it"),
                           why="everything after this is checked against what "
                               "is written here"))
        # Blocking questions first. An optional one listed ahead of the
        # confirmation makes any caller that reads asks[0] show the wrong
        # thing -- and "anywhere in particular to stay?" is a strange chip to
        # offer under "have I got this right?".
        return sorted(out, key=lambda a: a.optional)

    @property
    def ready(self) -> bool:
        return not [a for a in self.gaps() if not a.optional]

    @property
    def settled(self) -> tuple:
        """Only the parts a traveller can answer.

        `raw` holds whatever was last typed and changes every turn, so
        comparing whole Appointments to ask "did that tell me anything?"
        answers yes to everything.
        """
        return (self.what, self.who, self.where, self.place,
                self.day, self.when, self.minutes, self.confirmed)

    @property
    def ends(self) -> datetime | None:
        return self.when + timedelta(minutes=self.minutes) if self.when else None

    #: Words that name the shape of a meeting without naming the meeting.
    GENERIC = ("meeting", "appointment", "call", "lunch", "dinner", "coffee",
               "breakfast", "drinks", "catch-up", "catch up", "review")

    def title(self) -> str:
        """A title somebody would recognise in a clash report.

        Composed at render time rather than at parse time, because who it is
        with is usually answered several turns after the word "meeting" was
        typed. Fixing the title when the first word arrives leaves "Meeting"
        sitting in a clash line that would have read "Meeting with the Milan
        design team".
        """
        what = (self.what or "").strip()
        if what and what.lower() not in self.GENERIC:
            return what
        head = what.title() if what else "Meeting"
        return f"{head} with {self.who}" if self.who else (head or "Appointment")

    def summary(self) -> str:
        bits = [self.title()]
        if self.when:
            bits.append(f"{self.when:%d %b %H:%M}")
        elif self.day:
            bits.append(f"{self.day:%d %b}, time to confirm")
        if self.where:
            bits.append(self.where)
        return " · ".join(bits)

    def card(self) -> list[dict]:
        """What the system has recorded, laid out to be checked line by line.

        The time carries the city it is in. That is the whole reason this
        exists: "09:00" is not a fact until somebody says where, and a
        traveller reading "09:00, New York time" can catch in one second the
        mistake that otherwise surfaces as a clash with the wrong flight.
        """
        where = places.by_code(self.place)
        rows = [{"label": "What", "value": self.title()}]
        if self.who:
            rows.append({"label": "With", "value": self.who})
        rows.append({
            "label": "When",
            "value": (f"{self.when:%A %d %B, %H:%M}" if self.when
                      else f"{self.day:%A %d %B}" if self.day else "—"),
            "note": (f"{where.label} time" if where else ""),
        })
        rows.append({
            "label": "Where",
            "value": self.where or "—",
            "note": (f"{where.label} ({where.code})" if where
                     and where.label.lower() != (self.where or "").lower() else ""),
        })
        rows.append({"label": "For",
                     "value": (f"{self.minutes // 60}h {self.minutes % 60:02d}m"
                               if self.minutes >= 60 else f"{self.minutes} min")})
        return rows

    def to_booking(self, booking_id: str = "") -> Booking:
        """The appointment as the engine sees it: a place, a time, a promise."""
        return Booking(
            id=booking_id or f"appt-{abs(hash(self.summary())) % 10**8}",
            kind=Kind.ACTIVITY,
            provider=self.who or "appointment",
            title=self.title(),
            start=self.when,
            end=self.ends,
            origin=self.place or self.where,
            price=0.0,
            fixed_slot=True,
            commitment=True,
            who=self.who,
        )


# --------------------------------------------------------------------------
# reading one out of a sentence
# --------------------------------------------------------------------------


def _clock(text: str) -> time | None:
    m = _TIME.search(text)
    if not m:
        return None
    if m.group(1):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        suffix = (m.group(3) or "").lower().replace(".", "")
        if suffix.startswith("p") and hour != 12:
            hour += 12
        if suffix.startswith("a") and hour == 12:
            hour = 0
    else:
        hour, minute = int(m.group(4)), int(m.group(5))
    return time(hour, minute) if hour < 24 and minute < 60 else None


def _minutes(text: str) -> int:
    m = _DURATION.search(text)
    if not m:
        return DEFAULT_MINUTES
    amount, unit = float(m.group(1)), m.group(2).lower()
    total = amount * 60 if unit.startswith(("h", "hr")) else amount
    return max(5, min(int(total), 12 * 60))


def parse(text: str, today: date | None = None) -> Appointment:
    """Free text -> whatever can be established. Never a guess about the three."""
    today = today or date.today()
    low = (text or "").lower()

    day, _second = _dates(low, today)
    clock = _clock(text)
    when = datetime.combine(day, clock) if (day and clock) else None

    who = ""
    m = _WITH.search(text)
    if m:
        who = " ".join(m.group(1).split())[:60]

    where, place = "", ""
    for m in _AT_PLACE.finditer(text):
        candidate = " ".join(m.group(1).split())
        # "at 10am" is a time, not a place; the clock has already claimed it.
        if _TIME.match(candidate) or candidate.lower() in MONTHS:
            continue
        where = candidate[:60]
        found = places.find(where)
        place = found.code if found else ""
        break

    what = ""
    lead = re.match(r"\s*(?:i\s+have\s+an?\s+|there'?s\s+an?\s+|add\s+an?\s+)?"
                    r"([a-z][\w\- ]{2,40}?)\s+(?:with|on|at|in)\b", text, re.I)
    if lead:
        what = " ".join(lead.group(1).split()).strip()[:60]
        if what.lower() in ("meeting", "appointment", "call", "dinner", "lunch"):
            what = f"{what.title()}" + (f" with {who}" if who else "")

    return Appointment(what=what, who=who, where=where, place=place,
                       when=when, day=day, minutes=_minutes(text), raw=text)


_PROMPT = """Read this appointment and return ONLY JSON.

Text: {text}
Today: {today}

Keys, null where the text does not say:
  what      short label, e.g. "Design review"
  who       the person or team it is with
  where     the place, as written
  day       YYYY-MM-DD
  at        HH:MM in 24-hour time
  minutes   how long, as an integer

Do not infer. "Sometime next week" is not a day; "their office" is a where.
"""


def enrich(base: Appointment, model, today: date | None = None) -> Appointment:
    """Blanks only, and never a correction -- the same contract as requests."""
    if model is None or base.ready:
        return base
    today = today or date.today()
    try:
        reply = model.invoke(_PROMPT.format(text=base.raw, today=today.isoformat()))
        body = getattr(reply, "content", reply)
        if isinstance(body, list):
            body = "".join(p.get("text", "") for p in body if isinstance(p, dict))
        data = json.loads(re.search(r"\{.*\}", str(body), re.S).group(0))
    except Exception as exc:                          # noqa: BLE001
        import _model

        _model.note_failure(exc)
        return base

    changes: dict = {}
    if not base.what and data.get("what"):
        changes["what"] = str(data["what"])[:60]
    if not base.who and data.get("who"):
        changes["who"] = str(data["who"])[:60]
    if not base.where and data.get("where"):
        changes["where"] = str(data["where"])[:60]
        found = places.find(str(data["where"]))
        if found:
            changes["place"] = found.code
    if base.day is None and data.get("day"):
        try:
            changes["day"] = date.fromisoformat(str(data["day"])[:10])
        except ValueError:
            pass
    settled_day = changes.get("day", base.day)
    if base.when is None and settled_day and data.get("at"):
        clock = _clock(str(data["at"])) or _clock(str(data["at"]) + "am")
        if clock:
            changes["when"] = datetime.combine(settled_day, clock)
    if isinstance(data.get("minutes"), int) and base.minutes == DEFAULT_MINUTES:
        changes["minutes"] = max(5, min(data["minutes"], 12 * 60))
    return replace(base, **changes) if changes else base


def answer(base: Appointment, field: str, value: str,
           today: date | None = None) -> Appointment:
    """One answer, one slot. Unreadable answers leave it open, as everywhere."""
    today = today or date.today()
    value = (value or "").strip()
    if not value:
        return base

    if field == "who":
        return replace(base, who=value[:60])
    if field == "where":
        found = places.find(value)
        return replace(base, where=value[:60], place=found.code if found else "")
    if field == "place":
        found = places.find(value)
        return replace(base, place=found.code) if found else base
    if field == "confirm":
        if re.match(r"\s*(y|yes|yeah|yep|correct|right|ok|okay|save|go)", value, re.I):
            return replace(base, confirmed=True)
        return base                       # anything else is a correction
    if field == "day":
        day, _ = _dates(value.lower(), today)
        if not day:
            return base
        when = (datetime.combine(day, base.when.time()) if base.when else None)
        return replace(base, day=day, when=when)
    if field == "at":
        if base.day is None:
            return base
        clock = _clock(value)
        if clock is None:
            # Named parts of the day, resolved to a stated hour rather than a
            # vague one. "Morning" has to become a number before anything can
            # be checked against a flight, and the number is shown.
            named = {"morning": time(9, 0), "afternoon": time(14, 0),
                     "evening": time(19, 0), "lunch": time(12, 30),
                     "midday": time(12, 0), "noon": time(12, 0)}
            for word, at in named.items():
                if word in value.lower():
                    clock = at
                    break
        if clock is None:
            return base
        return replace(base, when=datetime.combine(base.day, clock))
    if field == "minutes":
        return replace(base, minutes=_minutes(value))
    return base


# --------------------------------------------------------------------------
# which trip this belongs to
# --------------------------------------------------------------------------


def candidates(appt: Appointment, trips: list[tuple[str, object]]) -> list[str]:
    """Which stored itineraries could contain this appointment.

    Matched on the day first, because a day is the thing a traveller always
    knows and a place is the thing they often give as "their office". A trip
    spans its first departure to its last arrival, and an appointment on a day
    inside that span is a candidate.

    Place narrows it only when the appointment named somewhere the engine
    recognises. "Their office" resolves to nothing, and narrowing on nothing
    would silently exclude the right trip -- so an unresolved place widens
    rather than filters, and the traveller is asked which trip they meant.
    """
    if appt.day is None:
        return []


    same_day = []
    for trip_id, trip in trips:
        rows = [b for b in trip.in_order() if b.start]
        if not rows:
            continue
        starts = min(b.start for b in rows).date()
        ends = max((b.end or b.start) for b in rows).date()
        if starts <= appt.day <= ends:
            same_day.append((trip_id, trip))

    if appt.place and len(same_day) > 1:
        here = [t for t in same_day
                if any(b.where == appt.place or b.destination == appt.place
                       for b in t[1].in_order())]
        if here:
            return [trip_id for trip_id, _ in here]
    return [trip_id for trip_id, _ in same_day]


def zone_for(appt: Appointment, trip):
    """The clock the meeting is actually on.

    "9am" means nine in the morning WHERE THE MEETING IS, and the first version
    of this took the timezone of the trip's first booking. On a trip that
    crosses timezones that is the wrong end of the journey: a 09:00 meeting in
    New York, on an itinerary starting in Singapore, was stamped 09:00 +08:00
    and silently became 21:00 the previous evening in New York. Twelve hours
    out, and it presented as a clash with the flight the traveller had already
    landed from.

    Three sources, best first:

      1. the place itself, when the traveller named somewhere the engine knows;
      2. where they will be that day -- the arrival timezone of the last thing
         that starts on or before it, which is the clock they are living on;
      3. the first booking, which is only ever a last resort.
    """
    from zoneinfo import ZoneInfo

    known = places.by_code(appt.place) if appt.place else None
    if known:
        try:
            return ZoneInfo(known.zone)
        except Exception:                                   # noqa: BLE001
            pass
    # Below here is a fallback that should now be unreachable for anything the
    # traveller confirmed: `gaps` will not call an appointment complete until
    # its city resolves. Kept because the engine is also handed appointments
    # from storage and from tests, and a guess that announces itself beats an
    # exception in a clash check.

    rows = [b for b in trip.in_order() if b.start]
    if not rows:
        return None
    if appt.day:
        before = [b for b in rows if b.start.date() <= appt.day]
        if before:
            landed = before[-1]
            return (landed.end or landed.start).tzinfo
    return rows[0].start.tzinfo


def localise(appt: Appointment, trip) -> Appointment:
    """Put the appointment on its own clock.

    Everything in an itinerary is timezone-aware because a Zurich arrival
    rendered in Singapore time is a different flight. A naive datetime compared
    to an aware one does not quietly do the wrong thing -- Python raises -- so
    this is the seam where it has to be settled rather than a bug waiting for
    the first appointment somebody adds.
    """
    if appt.when is None or appt.when.tzinfo is not None:
        return appt
    zone = zone_for(appt, trip)
    return appt if zone is None else replace(appt, when=appt.when.replace(tzinfo=zone))


def attach(trip, appt: Appointment):
    """The trip with this appointment in it, in the order things happen."""
    from domain import Trip as _Trip

    booking = localise(appt, trip).to_booking()
    kept = [b for b in trip.in_order() if b.id != booking.id]
    return _Trip(sorted(kept + [booking], key=lambda b: b.start))


def assess(trip, appt: Appointment) -> dict:
    """Can this be attended, and what does it collide with.

    Reported rather than refused. An appointment that does not fit is still an
    appointment the traveller has -- telling them it clashes is the product;
    declining to record it would just move the clash somewhere we cannot see.
    """
    from builder import clashes, unplaced

    combined = attach(trip, appt)
    found = clashes(combined)
    mine = [c for c in found if appt.title() in c]
    notes = unplaced(combined)
    return {
        # Judged on what is known. A location the engine does not recognise is
        # a gap in what we can check, not a verdict -- see `unplaced`.
        "feasible": not mine,
        "clashes": found,
        "about_this": mine,
        "notes": notes,
        "trip": combined,
    }
