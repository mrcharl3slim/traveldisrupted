"""Who may see and do what on a trip -- by what the job needs, not by title.

THERE IS NO LOGIN. Identity here is a token somebody was handed: the owner
shares a trip with "Priya, assistant" and gets back a link with a token in it,
and whoever holds that link is Priya as far as this system is concerned. That
is the honest shape for a product with no accounts, and it is said plainly
rather than dressed up -- a token in a URL is a key, and a key can be copied.
Without a token a request is the anonymous traveller, exactly as every request
was before this file existed, so nothing already working needs one.

CAPABILITIES, THEN ROLES. The brief names eight roles -- traveller, spouse,
family organiser, executive assistant, corporate travel manager, finance
approver, destination host, emergency contact -- and "each sees and can do
only what is necessary". Necessary is a set of capabilities, and a role is a
name for a set; naming the sets first means a host and an emergency contact
can share one set without the code pretending they are the same person.

    view     the itinerary: what, where, when, and what is broken
    money    prices, refunds, the ledger -- everything with a currency on it
    act      take or decline a plan, delay or cancel a leg, change the rules
    book     add to the trip: a meeting, a leg
    abandon  call the whole trip off
    share    add and remove other people

REDACTION IS BY KEY, NOT BY ENDPOINT. A role without `money` gets the same
payloads with every monetary field blanked, walked recursively, so a new
endpoint cannot leak a price by forgetting to strip it. The list of money keys
is the one thing to keep current when a field is added, and there is a test
that walks a full disruption payload for a host to catch it if it is not.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

VIEW, MONEY, ACT, BOOK, ABANDON, SHARE = "view", "money", "act", "book", "abandon", "share"

ROLES: dict[str, frozenset[str]] = {
    # The traveller. Everything, including who else gets to see this.
    "owner": frozenset({VIEW, MONEY, ACT, BOOK, ABANDON, SHARE}),
    # An executive assistant: runs the trip on the traveller's behalf, up to
    # and including calling it off. Does not decide who else is let in.
    "assistant": frozenset({VIEW, MONEY, ACT, BOOK, ABANDON}),
    # A spouse or family organiser, or a corporate travel manager: keeps the
    # trip moving and sees what it costs. Cancelling the whole thing is the
    # traveller's call, or their assistant's.
    "organiser": frozenset({VIEW, MONEY, ACT, BOOK}),
    # Finance: sees every number and can raise or lower the cap the agent
    # acts within, and cannot take a plan -- approving money is not spending it.
    "finance": frozenset({VIEW, MONEY, "rules"}),
    # Somebody meeting the traveller at the other end: needs to know when and
    # where, and has no business knowing what the room cost.
    "host": frozenset({VIEW}),
    # Somebody to call if it goes badly: where the traveller is and whether
    # something is wrong. The same set as a host, kept as its own name because
    # "I have shared this with my emergency contact" has to read back as that.
    "contact": frozenset({VIEW}),
}

#: `rules` is a capability of its own so finance can set the cap without being
#: able to take a plan. Owners, assistants and organisers have it through ACT.
RULES = "rules"


def can(role: str, capability: str) -> bool:
    caps = ROLES.get(role, frozenset())
    if capability == RULES:
        return RULES in caps or ACT in caps
    return capability in caps


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    token: str = ""

    @property
    def anonymous(self) -> bool:
        return self.id == "anon"


#: Whoever turned up with no token. One shared identity, which is what the app
#: had before roles existed; every trip made without a token belongs to it.
ANON = Person("anon", "the traveller")


def new_person(name: str) -> Person:
    """A person is a name and an unguessable token. The token is the whole of
    the security, so it is as long as an itinerary id and minted the same way."""
    clean = " ".join((name or "").split())[:60] or "someone"
    return Person(secrets.token_urlsafe(9), clean, secrets.token_urlsafe(18))


#: Every key that carries money, anywhere in a payload. A role without `money`
#: sees these as null. Kept as one list on purpose -- see the module docstring.
MONEY_KEYS = frozenset({
    "price", "total", "paid", "refund", "lost", "net", "net_cash", "total_damage",
    "wasted", "at_risk", "cash_in", "cash_out", "exposure", "recoverable",
    "do_nothing", "act_now", "saved", "worth", "fee", "change_fee", "quoted",
    "auto_limit", "price_source", "currency", "estimated",
})


def redact(value, role: str):
    """The same payload, with every monetary field blanked for a role that may
    not see money. Walks lists and dicts; leaves everything else alone."""
    if can(role, MONEY):
        return value
    if isinstance(value, dict):
        return {k: (None if k in MONEY_KEYS else redact(v, role)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, role) for v in value]
    return value


def role_of(payload: dict, owner: str, person: Person) -> str:
    """This person's role on this trip, or empty for no access at all.

    THE ANONYMOUS EXCEPTION, which is the whole reason this function is not two
    lines. `ANON.id` is the constant "anon", shared by every visitor who
    arrives without a token -- so while trips could be owned by "anon", this
    comparison returned "owner" to STRANGERS. Not an edge case: the front door
    sends no token, so it was the ordinary path. One visitor pasted an
    itinerary and the next visitor to open the URL was its owner: read the
    names and prices, mint themselves a share link, cancel the trip.

    Anonymity and ownership are therefore mutually exclusive. A browser that
    wants to own something asks for an identity first (POST /api/people); this
    is the backstop that makes forgetting to do so a broken flow rather than a
    silent leak, and it holds even for trips a previous deployment stored under
    "anon".
    """
    if person.id == owner and not person.anonymous:
        return "owner"
    for m in payload.get("members") or []:
        if m.get("person") == person.id:
            return str(m.get("role") or "")
    return ""
