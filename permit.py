"""What the agent may do on its own, as the traveller defined it.

Two questions decide whether an action happens without a click, and until this
file the engine asked only the first. CAN we -- does the provider expose a way
to do it? That is `plan.Lane`, a fact about the supplier: an email to a hotel
is AUTO, a fare is a TAP into the carrier's checkout, a SWISS cancellation is a
phone call. MAY we -- has the traveller allowed it? That is this file, a fact
about the person, and it is the one the brief means by "autonomous": nothing
here is autonomous because the software is capable of it, only because
somebody said it could.

WHAT PRE-AUTHORISED MEANS HERE, PRECISELY. This product cannot buy anything --
every provider account is a test account, deliberately -- so "pre-authorised"
cannot mean "we charged your card" and does not pretend to. It means the agent
TAKES THE DECISION without waiting: the plan is applied, everything in the
AUTO lane is sent, the itinerary is rewritten, and the purchase link is
waiting when the traveller looks. What was already going to need a person --
the tap, the call -- still does. The difference is the one the brief is
about: whether a two-hour delay at 02:00 is answered at 02:00 or at 07:30
when somebody wakes up and presses a button.

THE RULES ARE SMALL ON PURPOSE. A spending cap, a list of things never to
choose, and a list of verbs that always need a person. Three rules cover the
brief's "may do without approval, spending limits, prohibited choices, when it
must seek confirmation", and a fourth would be a rule nobody has asked for
yet. They live on the trip, because there is no traveller profile to live on;
the moment there is one, this dataclass moves and nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plan import Action, Lane, Plan

#: What the agent may do about one action, once both questions are answered.
AUTO = "auto"                       # nothing to approve: no money, reversible
PRE_AUTHORISED = "pre-authorised"   # within the rules; taken without asking
NEEDS_APPROVAL = "needs approval"   # over a limit or on the always-ask list
BLOCKED = "blocked"                 # a choice the traveller has ruled out


@dataclass(frozen=True)
class Permissions:
    #: What the agent may commit, per plan, without asking. Zero -- the
    #: default -- means every plan that costs money waits for a person, which
    #: is exactly the behaviour before this file existed. Nothing becomes
    #: autonomous by omission.
    auto_limit: float = 0.0
    currency: str = "EUR"
    #: Suppliers or modes the agent must never choose: "rail", "ryanair".
    #: Matched case-insensitively against the carrier a plan would buy from
    #: and the mode it travels by -- what the agent would CHOOSE, not what the
    #: trip already contains. "never Booking.com" must not stop the agent
    #: phoning the Booking.com hotel the traveller already holds.
    never: tuple[str, ...] = ()
    #: Verbs that need a person however cheap. "cancel" is the obvious one --
    #: irreversible -- and it is not the default, because in this product a
    #: cancellation is already a tap or a call and never performed by us.
    always_ask: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Permissions":
        """Tolerant on purpose: a stored trip predating this file has no
        permissions block, and that has to mean "nothing pre-authorised", not
        a crash on every page that shows it."""
        raw = raw or {}
        try:
            limit = max(0.0, float(raw.get("auto_limit") or 0.0))
        except (TypeError, ValueError):
            limit = 0.0
        return cls(
            auto_limit=limit,
            currency=str(raw.get("currency") or "EUR"),
            never=tuple(_words(raw.get("never"))),
            always_ask=tuple(_words(raw.get("always_ask"))),
        )

    def to_dict(self) -> dict:
        return {"auto_limit": self.auto_limit, "currency": self.currency,
                "never": list(self.never), "always_ask": list(self.always_ask)}

    @property
    def anything(self) -> bool:
        """Has the traveller allowed the agent to act on its own at all?"""
        return self.auto_limit > 0


def _words(value) -> list[str]:
    """A list, or a comma-separated string, as lower-cased words."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [w.strip().lower() for w in value if str(w).strip()]


@dataclass(frozen=True)
class Verdict:
    """One plan, judged. `approvals` is per action, keyed by label because an
    action has no id of its own and the label is what the page shows."""

    allowed: bool                     # not ruled out by `never`
    auto: bool                        # may be taken without asking
    approvals: dict[str, tuple[str, str]] = field(default_factory=dict)
    why: str = ""                     # one sentence a traveller can act on

    def approval(self, action: Action) -> tuple[str, str]:
        return self.approvals.get(action.label, (NEEDS_APPROVAL, ""))


def _blocked(plan: Plan, perms: Permissions) -> str:
    """The rule a plan breaks, or empty."""
    # Lower-cased HERE, not only in from_dict: a Permissions built in code
    # with never=("SWISS",) has to mean the same as one read from a form.
    for word in (w.lower().strip() for w in perms.never if w):
        if plan.mode and word == plan.mode:
            return f"you said never {word}"
        for a in plan.actions:
            if a.verb == "buy" and a.provider and word in a.provider.lower():
                return f"you said never {a.provider}"
    return ""


def judge(plan: Plan, perms: Permissions) -> Verdict:
    """Both questions, for every action, and a sentence for the plan.

    A plan is `auto` when nothing in it needs a person: every action is either
    in the AUTO lane already, or is pre-authorised by the cap and not on the
    always-ask list. A plan with a CALL in it can still be auto -- the call was
    always going to be theirs; what the cap decides is whether the agent waits
    for them to press a button before doing its own part.
    """
    blocked = _blocked(plan, perms)
    approvals: dict[str, tuple[str, str]] = {}
    ask = False

    for a in plan.actions:
        if blocked:
            approvals[a.label] = (BLOCKED, blocked)
            continue
        if a.lane is Lane.AUTO:
            approvals[a.label] = (AUTO, "no money moves and nothing is irreversible")
            continue
        if a.verb in perms.always_ask:
            approvals[a.label] = (NEEDS_APPROVAL, f"you asked to be asked before any {a.verb}")
            ask = True
            continue
        if plan.cash_out <= perms.auto_limit:
            approvals[a.label] = (
                PRE_AUTHORISED,
                f"{perms.currency} {plan.cash_out:,.0f} is within your "
                f"{perms.currency} {perms.auto_limit:,.0f} limit")
            continue
        approvals[a.label] = (
            NEEDS_APPROVAL,
            f"{perms.currency} {plan.cash_out:,.0f} exceeds your "
            f"{perms.currency} {perms.auto_limit:,.0f} limit"
            if perms.anything else "you have not pre-authorised any spending")
        ask = True

    if blocked:
        return Verdict(False, False, approvals, blocked)
    if plan.id == "noop":
        # Inaction is never "taken". It is what happens when nothing is.
        return Verdict(True, False, approvals, "")
    if not ask:
        return Verdict(True, True, approvals,
                       f"within your {perms.currency} {perms.auto_limit:,.0f} limit"
                       if plan.cash_out else "costs nothing")
    over = [why for kind, why in approvals.values() if kind == NEEDS_APPROVAL]
    return Verdict(True, False, approvals, over[0] if over else "")


def permitted(plans: list[Plan], perms: Permissions) -> tuple[list[Plan], list[Plan]]:
    """Split a ranking into what may be offered and what the traveller ruled
    out. The ruled-out ones are returned rather than dropped, so the page can
    say "two rail options were not shown because you said never rail" instead
    of leaving a traveller to wonder why the train is missing."""
    keep, out = [], []
    for p in plans:
        (keep if judge(p, perms).allowed else out).append(p)
    return keep, out
