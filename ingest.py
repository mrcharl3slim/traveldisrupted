"""Confirmation emails -> Bookings the engine can reason about.

THE BLOCKER THIS REMOVES. Everything downstream of `domain.Trip` works. Nothing
upstream of it exists: the demo trip is a literal, and a product whose input is
a Python file has no users. This is the piece between a working engine and a
real one.

WHY A MODEL. A booking confirmation is prose with a layout, and every airline,
hotel and rail operator invents its own. Regexes reach maybe the flight number
and the date; they do not reach "your Economy Classic fare is non-refundable
but taxes may be reclaimed", or notice that the pickup address is the hotel
from a different email.

THREE RULES, ALL OF WHICH EXIST BECAUSE THE MODEL WILL SOMETIMES BE WRONG.

1. **Every field quotes its source.** Each booking keeps the line it came from.
   When the engine later says EUR 142 dies at 08:20, the sentence that justifies
   it travels with the claim and can be shown to whoever doubts it.

2. **Extraction is checked against physics.** A trip where the traveller lands
   in Zurich after the train they are booked on has left is not a trip; it is a
   parse error wearing a trip's clothes. `validate` catches ordering, overlap
   and impossible ground transit, and those become warnings a human resolves —
   never silent corrections.

3. **Unparseable is dropped, never guessed.** A booking the model is unsure of
   is returned as a problem, not as a Booking with plausible defaults. The
   engine treats missing fare rules as non-refundable, so an invented refund is
   the one error that costs a traveller money.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from domain import Booking, Kind, Trip, transit
from parse import parse_policy
from policy import Policy, Window

SYSTEM = """You read travel booking confirmations and extract structured bookings.

Return ONLY a JSON object:
{"bookings": [{
   "kind": "flight"|"rail"|"lodging"|"transfer"|"activity",
   "provider": "<the company>",
   "title": "<short human label, e.g. 'SQ 346 - Singapore to Zurich'>",
   "start": "<ISO 8601 with offset, e.g. 2026-10-11T23:55:00+08:00>",
   "end": "<ISO 8601 with offset, or null>",
   "origin": "<IATA, station or city code, or null>",
   "destination": "<same, or null>",
   "price": <number or null>,
   "currency": "<ISO code>",
   "ticket_group": "<booking reference / PNR, or null>",
   "fare_rules": "<the provider's own wording about changes and refunds, or null>",
   "source": "<the exact line(s) you took the times from>"
 }],
 "problems": ["<anything you could not read, one clause each>"]}

Rules:
- Times MUST carry the local UTC offset of the place they happen. A departure
  time with no offset is unusable; if the confirmation does not state a zone and
  you cannot infer it from the airport or city, put the booking in "problems"
  instead of guessing.
- ticket_group is the booking reference. Two flights on the SAME reference are
  one contract; two on different references are two contracts and nobody owes a
  connection. Never merge or invent references.
- fare_rules: quote the provider. Do not paraphrase "non-refundable" into
  something kinder, and do not summarise a rule you only half-read.
- price is what was PAID, not a fare quoted elsewhere in the email.
- If a field is not stated, use null. Never fill a gap with a typical value.
- source must be text that actually appears in the input."""

#: The model's vocabulary mapped onto the engine's. A kind outside this table
#: becomes LODGING-free ACTIVITY rather than a new enum member invented at
#: runtime — the engine's deadline rules are written per kind, and an unknown
#: kind would silently get flight boarding buffers.
KINDS = {"flight": Kind.FLIGHT, "rail": Kind.RAIL, "hotel": Kind.LODGING,
         "lodging": Kind.LODGING, "transfer": Kind.TRANSFER,
         "event": Kind.ACTIVITY, "activity": Kind.ACTIVITY}
DEFAULT_KIND = Kind.ACTIVITY


@dataclass
class Extraction:
    """What came out, and everything that did not."""

    trip: Trip
    problems: list[str]
    sources: dict[str, str]          # booking id -> the line it came from
    warnings: list[str]              # physics the extraction fails

    @property
    def ok(self) -> bool:
        return bool(self.trip.bookings) and not self.problems and not self.warnings


def _dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # A naive timestamp is the failure this rule exists for: it silently becomes
    # the server's timezone, which is a different flight.
    return value if value.tzinfo else None


def _windows(raw) -> list[Window]:
    """Stored windows -> Windows. Anything malformed is dropped, not repaired."""
    out: list[Window] = []
    for item in raw or []:
        closes = _dt(item.get("closes"))
        if closes is None:
            continue
        out.append(Window(closes=closes,
                          refund=float(item.get("refund") or 0.0),
                          fee=float(item.get("fee") or 0.0),
                          label=str(item.get("label") or "")))
    return out


def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "", text.lower())[:12] or "booking"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}{n}", n + 1
    return candidate


def to_bookings(raw: dict) -> tuple[list[Booking], list[str], dict[str, str]]:
    """Model output -> Bookings, dropping anything that does not survive checks."""
    bookings: list[Booking] = []
    problems = list(raw.get("problems") or [])
    sources: dict[str, str] = {}
    taken: set[str] = set()

    for item in raw.get("bookings") or []:
        title = str(item.get("title") or "").strip()
        start = _dt(item.get("start"))
        if not title or start is None:
            problems.append(
                f"dropped {title or 'an unnamed booking'}: "
                + ("no start time" if not start else "no title")
                + " with a usable timezone")
            continue

        kind = KINDS.get(str(item.get("kind") or "").lower(), DEFAULT_KIND)
        bid = _slug(title, taken)
        taken.add(bid)
        if item.get("source"):
            sources[bid] = str(item["source"])[:300]

        bookings.append(Booking(
            id=bid,
            kind=kind,
            provider=str(item.get("provider") or "unknown"),
            title=title,
            start=start,
            end=_dt(item.get("end")),
            origin=item.get("origin") or None,
            destination=item.get("destination") or None,
            price=float(item.get("price") or 0.0),
            currency=str(item.get("currency") or "EUR"),
            ticket_group=item.get("ticket_group") or None,
            # Engine-side fields. The model never supplies these; they survive a
            # storage round trip and are absent from a fresh extraction, which
            # is correct — a confirmation email does not state that a hotel will
            # hold the room until 22:00, that is knowledge the product adds.
            fixed_slot=bool(item.get("fixed_slot")),
            hard_deadline=_dt(item.get("hard_deadline")),
            mitigation=item.get("mitigation") or None,
            pending=bool(item.get("pending")),
            # Windows arrive already resolved when reading a stored trip, and
            # empty when reading a fresh extraction — `resolve` fills those in
            # against each booking's own times. A Policy with no windows means
            # non-refundable, which under-promises rather than inventing a
            # refund that does not exist.
            policy=Policy(source=str(item.get("fare_rules") or ""),
                          windows=_windows(item.get("windows"))),
        ))

    bookings.sort(key=lambda b: b.start)
    return bookings, problems, sources


def validate(bookings: list[Booking]) -> list[str]:
    """Check the extraction against physics. Warnings, never corrections.

    A trip where the traveller lands after the train they are booked on has left
    is not a trip, it is a parse error. Saying so is useful; quietly adjusting a
    time to make it consistent would hide the very thing that needs a human.
    """
    warnings: list[str] = []
    for booking in bookings:
        if booking.end and booking.end < booking.start:
            warnings.append(f"{booking.title}: ends before it starts")

    for earlier, later in zip(bookings, bookings[1:]):
        left = earlier.end or earlier.start
        if later.start < left and earlier.kind is not Kind.LODGING:
            warnings.append(
                f"{later.title} starts before {earlier.title} finishes")
            continue
        leg = transit(earlier.destination or earlier.where, later.where)
        if leg is not None and left + leg > later.must_arrive_by:
            short = int(((left + leg) - later.must_arrive_by).total_seconds() // 60)
            warnings.append(
                f"{later.title} is unreachable from {earlier.title} "
                f"by {short} min — check the times or the timezone")
    return warnings


def _fallback(text: str) -> dict:
    """No model: report honestly rather than half-parse.

    A regex can find a flight number and a date. It cannot find a fare rule, a
    booking reference or a timezone, and a Trip missing those produces confident
    arithmetic about the wrong thing.
    """
    found = re.findall(r"\b([A-Z]{2}\s?\d{2,4})\b", text)
    return {"bookings": [], "problems": [
        "no model configured, so nothing was extracted"
        + (f" (text mentions {', '.join(sorted(set(found))[:5])})" if found else "")]}


def extract(text: str, model=None) -> Extraction:
    """Pasted or forwarded confirmations -> a checked Trip."""
    import _model

    raw = _model.ask_json(model, SYSTEM, text.strip()[:12000]) if model else None
    if not raw or not isinstance(raw.get("bookings"), list):
        raw = _fallback(text)

    bookings, problems, sources = to_bookings(raw)
    bookings = resolve(bookings, model)
    return Extraction(trip=Trip(bookings), problems=problems, sources=sources,
                      warnings=validate(bookings))


def resolve(bookings: list[Booking], model=None) -> list[Booking]:
    """Fare-rule prose -> deadlines, against each booking's own times.

    Extraction reads what the confirmation says; this turns "free change up to
    4 hours before pickup" into a timestamp. Kept separate because the two fail
    differently: a booking that cannot be read is a problem the traveller sees,
    while a fare rule that cannot be parsed is simply non-refundable — the safe
    direction, since inventing a refund is the error that costs money.
    """
    import dataclasses

    out = []
    for booking in bookings:
        prose = booking.policy.source if booking.policy else ""
        if not prose or (booking.policy and booking.policy.windows):
            out.append(booking)
            continue
        policy = parse_policy(prose, booking.start, booking.hard_deadline, model)
        out.append(dataclasses.replace(booking, policy=policy))
    return out


#: Booking fields that are deliberately not stored. ``id`` is regenerated from
#: the title on read, and ``policy`` is split into ``fare_rules`` plus resolved
#: ``windows``. Anything else missing from the round trip is a bug, and
#: test_ingest asserts exactly that — the first two versions of this serialiser
#: silently dropped fields, and each time the engine answered confidently with
#: the wrong number rather than failing.
NOT_STORED = {"id", "policy"}


def to_dicts(bookings: list[Booking]) -> list[dict]:
    """Bookings -> the shape ``to_bookings`` reads back.

    The storage format is the extraction format on purpose. One serialiser
    means a stored trip and a freshly parsed one cannot drift, and a round-trip
    test is enough to prove it.
    """
    return [{
        "kind": b.kind.value,
        "provider": b.provider,
        "title": b.title,
        "start": b.start.isoformat(),
        "end": b.end.isoformat() if b.end else None,
        "origin": b.origin,
        "destination": b.destination,
        "price": b.price,
        "currency": b.currency,
        "ticket_group": b.ticket_group,
        "fixed_slot": b.fixed_slot,
        "hard_deadline": b.hard_deadline.isoformat() if b.hard_deadline else None,
        "mitigation": b.mitigation,
        "pending": b.pending,
        "fare_rules": b.policy.source if b.policy else "",
        # Resolved windows travel with the trip. Without them a stored trip has
        # no deadlines and no recoverable value, so the engine reports every
        # booking as a total loss — which is what happened the first time this
        # was written, to the tune of EUR 906 instead of EUR 282.
        "windows": [{"closes": w.closes.isoformat(), "refund": w.refund,
                     "fee": w.fee, "label": w.label}
                    for w in (b.policy.windows if b.policy else [])],
    } for b in bookings]
