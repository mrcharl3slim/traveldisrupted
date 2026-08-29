"""Chosen offers -> a Trip the engine can reason about.

The demo trip was a literal. This is the same trip assembled from what a
traveller actually picked out of a search, which is the difference between a
scenario and a product.

TWO THINGS THIS GETS RIGHT THAT A LITERAL CANNOT.

**Separate purchases are separate contracts.** Each flight booked in its own
search gets its own ticket group, because that is what happened. Nobody has to
remember to encode the scenario's central flaw — two tickets, so no airline owes
the connection — it falls out of buying two tickets.

**Fare rules come from the airline.** Duffel states whether a fare can be
refunded or changed and what the penalty is. Reading those turns "assume
non-refundable" — the safe guess the engine makes when it knows nothing — into
the real number. Where the airline says nothing, the safe guess stands.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from domain import Booking, Kind, Trip, transit
from policy import Policy, Window

#: Time on the ground between a landing and the next departure. Not a comfort
#: preference — below this the connection is not sellable, and an itinerary
#: assembled without it is not a trip anybody could take.
MIN_CONNECTION = timedelta(minutes=90)


def flight(offer, seat: int = 1) -> Booking:
    """One purchased flight offer -> a Booking, with its real fare rules."""
    windows: list[Window] = []
    if offer.refundable:
        windows.append(Window(
            closes=offer.depart, refund=offer.price, fee=0.0,
            label="refundable before departure"))
    elif offer.change_fee is not None:
        # A changeable fare keeps its value minus the penalty: the traveller
        # does not get money back, they get to use it later. That is exactly the
        # refund/fee split policy.py already models.
        windows.append(Window(
            closes=offer.depart, refund=offer.price, fee=offer.change_fee,
            label=f"change before departure, fee {offer.currency} {offer.change_fee:,.0f}"))

    source = ("refundable before departure" if offer.refundable
              else f"changeable before departure for {offer.currency} {offer.change_fee:,.0f}"
              if offer.change_fee is not None
              else "non-refundable, no changes permitted")

    return Booking(
        # Short enough to read in a table, taken from the END of the provider's
        # id because the beginning is a constant prefix.
        id=f"f{offer.id[-8:]}".lower(),
        kind=Kind.FLIGHT,
        provider=offer.carrier,
        title=offer.label,
        start=offer.depart,
        end=offer.arrive,
        origin=offer.origin,
        destination=offer.destination,
        price=offer.price,
        currency=offer.currency,
        # Each offer was bought on its own, so each is its own contract. Two
        # flights from two searches will never share this, which is the whole
        # premise of the scenario arriving by itself.
        ticket_group=offer.id,
        policy=Policy(source=source, windows=windows),
    )


def stay(hotel: dict, place: str = "") -> Booking:
    """One chosen hotel rate -> a Booking, with the deadline that matters.

    The arrival guarantee is not in any rate response. It is the difference
    between a late arrival being a phone call and being a lost room, so it is
    set here and marked as defusable by telling somebody — which is what makes
    the engine call it at risk rather than broken.
    """
    return Booking(
        id=f"h{hotel['id'][-8:]}".lower(),
        kind=Kind.LODGING,
        provider=hotel.get("provider", "hotel"),
        title=f"{hotel['name']} - {hotel['nights']} night"
              + ("s" if hotel["nights"] != 1 else ""),
        start=hotel["check_in"],
        end=hotel["check_out"],
        # Where the hotel is, as a code the rest of the system can search on.
        # LiteAPI answers "Milan", which is a fine thing to print and useless as
        # a search origin — every replacement query would ask an airline for
        # flights to a word.
        origin=place or hotel.get("city_code") or hotel.get("city", ""),
        price=hotel["price"],
        currency=hotel["currency"],
        hard_deadline=hotel["arrival_guarantee"],
        mitigation="call the property to hold the room",
        policy=Policy(source="rate conditions not supplied by the search"),
    )


def assemble(flights: list, hotels: list) -> Trip:
    """Selections -> a Trip, in the order they happen.

    A hotel is placed where the hotel is: the code its own search was pointed
    at. Only when the search did not carry one does this fall back to the
    airport the traveller last flew into -- and that fallback is a guess, kept
    because it is better than an empty string and dangerous enough to be worth
    naming. It reads the itinerary to decide where a building is, so an onward
    flight landing the next morning files a Milan hotel under Zurich, and every
    consequence after that is confidently wrong.
    """
    legs = sorted(flights, key=lambda o: o.depart)
    bookings = [flight(o) for o in legs]

    for h in hotels:
        before = [o for o in legs if o.arrive <= h["arrival_guarantee"]]
        bookings.append(stay(h, h.get("city_code")
                             or (before[-1].destination if before else "")))

    return Trip(sorted(bookings, key=lambda b: b.start))


def onward_after(previous) -> datetime:
    """Earliest a following flight may depart, given the one before it.

    Search constraints belong here rather than in the caller: "cheapest of each
    leg" assembles a trip whose second flight leaves twelve hours before the
    first one lands. Every leg after the first is searched from when the
    traveller is actually on the ground.
    """
    return previous.arrive + MIN_CONNECTION


def infeasible(trip: Trip) -> list[str]:
    """Why this selection could not be taken. Empty means it could.

    The same physics `ingest` applies to a pasted itinerary, applied to one
    being assembled — a booking flow that lets somebody buy an impossible trip
    has failed before the engine ever sees it.
    """
    problems: list[str] = []
    ordered = trip.in_order()
    for earlier, later in zip(ordered, ordered[1:]):
        # Lodging chains with nothing. It is not a place the traveller must
        # depart from, and check-in opening at 14:00 is not a commitment to be
        # there at 14:00 — only the arrival guarantee is a deadline. Treating a
        # hotel as a leg produced "no way to get from Milan to ZRH", which is
        # true and irrelevant.
        if Kind.LODGING in (earlier.kind, later.kind):
            continue
        left = earlier.end or earlier.start
        leg = transit(earlier.destination or earlier.where, later.where)
        gap = later.start - left
        if leg is None and (earlier.destination or earlier.where) != later.where:
            problems.append(
                f"no way to get from {earlier.destination or earlier.where} "
                f"to {later.where} for {later.title}")
        elif gap < MIN_CONNECTION:
            minutes = int(gap.total_seconds() // 60)
            problems.append(
                f"{later.title} leaves {abs(minutes)} min "
                + ("before" if minutes < 0 else "after")
                + f" {earlier.title} lands — needs {int(MIN_CONNECTION.total_seconds() // 60)}")

    problems.extend(_unreachable_stays(ordered))
    return problems


#: Getting from an airport to an address the engine has never heard of. Coarse
#: on purpose, and stated rather than hidden: it is the last hop after the
#: transit table runs out.
LOCAL_HOP = timedelta(minutes=45)


def _span(b: Booking) -> tuple:
    """When the traveller has to be somewhere, and until when.

    Lodging is excluded by the caller rather than given a span: a room is
    somewhere you may be, not somewhere you must be, and treating a three-night
    stay as a three-day commitment makes every meeting in the trip a clash.
    """
    return b.start, (b.end or b.start)


def clashes(trip: Trip) -> list[str]:
    """Things that cannot both happen, and things there is no time to reach.

    Two rules, applied to everything with a time on it:

    **Overlap.** Two commitments in the same hours are one you will miss. This
    is the check an appointment needs and a purchase never did, which is why it
    was not here before -- nobody sells you two flights at once, and everybody
    accepts two meetings at once.

    **Reach.** Consecutive commitments in different places need the travel time
    between them to exist and to fit. `transit` returns None for pairs it does
    not know, and None means "there is no ground route", not "assume it is
    fine" -- the same rule the engine applies everywhere else.
    """
    out: list[str] = []
    ordered = [b for b in trip.in_order()
               if b.kind is not Kind.LODGING and b.start]

    for i, earlier in enumerate(ordered):
        for later in ordered[i + 1:]:
            (a_start, a_end), (b_start, b_end) = _span(earlier), _span(later)
            if b_start >= a_end:
                break                      # sorted: nothing after this overlaps
            # No exemption for "same place, same kind". Two meetings in the
            # same building at the same time is still two meetings, and the
            # first version let exactly that through — the case an appointment
            # feature exists to catch.
            out.append(
                f"{later.title} at {b_start:%H:%M} overlaps {earlier.title}, "
                f"which runs to {a_end:%d %b %H:%M}")

    for earlier, later in zip(ordered, ordered[1:]):
        if not later.commitment and not earlier.commitment:
            continue                       # purchases are `infeasible`'s job
        from_where = earlier.destination or earlier.where
        if not from_where or not later.where or from_where == later.where:
            continue
        leg = transit(from_where, later.where)
        if leg is None:
            out.append(f"no route the engine knows from {from_where} to "
                       f"{later.where} for {later.title}")
            continue
        arrive = (earlier.end or earlier.start) + leg + LOCAL_HOP
        if arrive > later.start:
            late = int((arrive - later.start).total_seconds() // 60)
            out.append(f"{later.title} starts {late} min before you could get "
                       f"there from {earlier.title}")
    return out


def _unreachable_stays(ordered: list) -> list[str]:
    """Rooms nobody can get to before the door stops being held.

    Lodging chains with nothing, which is why it is skipped above -- but "not a
    leg" is not the same as "not somewhere you have to be". A room held until
    22:00 and an onward flight that lands at 09:10 the next morning is a
    selection that cannot be taken, and until this ran the booking flow sold it
    and the engine reported no damage, because by then the hotel had been
    quietly relocated to the city the traveller was actually in.
    """
    out: list[str] = []
    for stay_ in [b for b in ordered if b.kind is Kind.LODGING]:
        if not stay_.where or stay_.where == "?":
            continue
        arrivals = [b for b in ordered
                    if b.kind is not Kind.LODGING
                    and (b.end or b.start) <= stay_.must_arrive_by]
        best = None
        for b in arrivals:
            leg = transit(b.destination or b.where, stay_.where)
            if leg is None:
                continue
            at = (b.end or b.start) + leg
            if best is None or at < best:
                best = at
        if best is None or best > stay_.must_arrive_by:
            out.append(
                f"nothing on this trip reaches {stay_.title} at {stay_.where} "
                f"before the room stops being held at "
                f"{stay_.must_arrive_by:%d %b %H:%M}")
    return out
