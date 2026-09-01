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
AUTO_HELD = "auto-held"             # taken, nothing sent, flagged for review
NEEDS_APPROVAL = "needs approval"   # over a limit or on the always-ask list
BLOCKED = "blocked"                 # a choice the traveller has ruled out

#: The one fixed reason the kill switch ever gives. Fixed on purpose: a switch
#: that explains itself differently per action invites arguing with it.
DISARMED = "the master kill switch is on — everything waits for a person"


@dataclass(frozen=True)
class Permissions:
    #: What the agent may commit, per plan, without asking. Zero -- the
    #: default -- means every plan that costs money waits for a person, which
    #: is exactly the behaviour before this file existed. Nothing becomes
    #: autonomous by omission.
    auto_limit: float = 0.0
    currency: str = "SGD"
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
    #: The second threshold. Between `auto_limit` and here the agent still
    #: DECIDES -- the plan is applied, the replacement enters as `pending`,
    #: which is what a hold is in a product that cannot pay: an intention
    #: recorded, not bought -- but nothing leaves the building. The outward
    #: sends are held exactly as the kill switch holds them, and the
    #: notification tells the traveller to review. Above it, nothing happens
    #: without a person. Zero -- the default -- means no middle tier: the
    #: tiers are what a traveller configures, not what the product ships,
    #: because shipping them would be shipping something that spends money
    #: out of the box.
    hold_limit: float = 0.0
    #: THE MASTER KILL SWITCH. On, and every action on every plan waits for a
    #: person -- the cap is ignored, the AUTO lane's sends are held, and the
    #: agent recommends and touches nothing. It lives here rather than as a
    #: subsystem because judge() is the single gate every plan already passes
    #: through: a flag and a short-circuit. Note what off means: auto_limit's
    #: default of zero is ALREADY manual mode for anything that costs money;
    #: disarm is for the rest -- the emails, the pre-authorised caps somebody
    #: set last week -- when a person wants the whole machine to stand still.
    disarmed: bool = False

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
        try:
            hold = max(0.0, float(raw.get("hold_limit") or 0.0))
        except (TypeError, ValueError):
            hold = 0.0
        return cls(
            auto_limit=limit,
            # A hold ceiling below the auto ceiling is a contradiction, not a
            # configuration; the auto tier wins and the middle tier is empty.
            hold_limit=max(hold, limit) if hold else 0.0,
            currency=str(raw.get("currency") or "SGD"),
            never=tuple(_words(raw.get("never"))),
            always_ask=tuple(_words(raw.get("always_ask"))),
            disarmed=bool(raw.get("disarmed")),
        )

    def to_dict(self) -> dict:
        return {"auto_limit": self.auto_limit, "hold_limit": self.hold_limit,
                "currency": self.currency,
                "never": list(self.never), "always_ask": list(self.always_ask),
                "disarmed": self.disarmed}

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
    auto: bool                        # may be taken without asking, sends included
    approvals: dict[str, tuple[str, str]] = field(default_factory=dict)
    why: str = ""                     # one sentence a traveller can act on
    #: The middle tier: taken without asking, sends held, review flagged.
    #: Last in the dataclass on purpose -- every older call site constructs
    #: positionally, and a field inserted mid-order silently reassigns them.
    hold: bool = False

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
    # The kill switch short-circuits everything, `never` included: a disarmed
    # machine does not filter the menu, it stops the kitchen. Every action --
    # the AUTO-lane email included -- waits for a person, with the one fixed
    # reason, and the plans stay visible because read-only means read.
    if perms.disarmed:
        approvals = {a.label: (NEEDS_APPROVAL, DISARMED) for a in plan.actions}
        return Verdict(True, False, approvals, DISARMED)

    blocked = _blocked(plan, perms)
    approvals: dict[str, tuple[str, str]] = {}
    ask = False

    for a in plan.actions:
        if blocked:
            approvals[a.label] = (BLOCKED, blocked)
            continue
        # Asked BEFORE the AUTO shortcut, or always_ask("notify") is a no-op:
        # notify is an AUTO-lane action -- the one AUTO verb that leaves the
        # building -- and the shortcut used to wave it through unexamined.
        if a.verb in perms.always_ask:
            approvals[a.label] = (NEEDS_APPROVAL, f"you asked to be asked before any {a.verb}")
            ask = True
            continue
        if a.lane is Lane.AUTO:
            approvals[a.label] = (AUTO, "no money moves and nothing is irreversible")
            continue
        if plan.cash_out <= perms.auto_limit:
            approvals[a.label] = (
                PRE_AUTHORISED,
                f"S${plan.cash_out:,.0f} is within your "
                f"S${perms.auto_limit:,.0f} limit")
            continue
        if plan.cash_out <= perms.hold_limit:
            approvals[a.label] = (
                AUTO_HELD,
                f"S${plan.cash_out:,.0f} is within your "
                f"S${perms.hold_limit:,.0f} auto-hold tier — taken and held, "
                "nothing sent, review it")
            continue
        approvals[a.label] = (
            NEEDS_APPROVAL,
            f"S${plan.cash_out:,.0f} exceeds your "
            f"S${max(perms.hold_limit, perms.auto_limit):,.0f} limit"
            if perms.anything else "you have not pre-authorised any spending")
        ask = True

    if blocked:
        return Verdict(False, False, approvals, blocked)
    if plan.id == "noop":
        # Inaction is never "taken". It is what happens when nothing is.
        return Verdict(True, False, approvals, "")
    if not ask:
        held = any(kind == AUTO_HELD for kind, _ in approvals.values())
        if held:
            return Verdict(True, False, approvals=approvals, hold=True,
                           why=f"S${plan.cash_out:,.0f} is within your "
                               f"S${perms.hold_limit:,.0f} auto-hold tier — "
                               "taken and held, nothing sent, review it")
        return Verdict(True, True, approvals,
                       f"within your S${perms.auto_limit:,.0f} limit"
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
