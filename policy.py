"""What a booking is worth at a given moment, and when that stops being true.

The engine's whole claim rests in this file. A confirmation email says "free
change up to 4 hours before pickup"; that sentence is worth EUR 48 at 05:59 on
12 October and EUR 0 at 06:01. Every figure Downstream puts in front of a
traveller is a policy evaluated at a timestamp -- never a number typed into a
demo.

WHY WINDOWS ARE STORED RESOLVED. A relative rule ("4 h before") is ambiguous
the moment the booking it hangs off moves, and recovery plans move bookings
constantly. So resolution to an absolute datetime happens once, at ingestion,
where the booking's own times are still in hand. What survives into the engine
is a deadline, not a sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Window:
    """An opportunity that expires.

    Acting before ``closes`` returns ``refund`` and costs ``fee``; afterwards
    the window contributes nothing at all. ``label`` is what we show the
    traveller and is quoted from the provider's own wording wherever we have
    it, because "no-show forfeits fare" is more persuasive in the airline's
    voice than in ours.
    """

    closes: datetime
    refund: float = 0.0
    fee: float = 0.0
    label: str = ""

    def open_at(self, now: datetime) -> bool:
        return now < self.closes

    @property
    def net(self) -> float:
        """What acting is actually worth, after whatever it costs to act."""
        return self.refund - self.fee


@dataclass
class Policy:
    """Every way out of a booking, and the deadline on each.

    ``source`` keeps the prose the windows were derived from. When an LLM is
    the thing turning "Economy Classic, no-show forfeits fare" into a Window,
    the original sentence is the only way anyone can check its work -- so it
    travels with the structured form rather than being discarded at parse time.
    """

    windows: list[Window] = field(default_factory=list)
    source: str = ""

    def open_windows(self, now: datetime) -> list[Window]:
        return [w for w in self.windows if w.open_at(now)]

    def best_window(self, now: datetime) -> Window | None:
        """The most valuable escape still available."""
        return max(self.open_windows(now), key=lambda w: w.net, default=None)

    def recoverable_at(self, now: datetime) -> float:
        """Money a traveller could still claw back -- if they act.

        Note the conditional. Nothing here happens by itself; every euro in
        this number requires somebody to do something before a deadline, which
        is precisely why doing nothing is expensive.
        """
        w = self.best_window(now)
        return max(0.0, w.net) if w else 0.0

    def next_cutoff(self, now: datetime) -> Window | None:
        """The soonest deadline still ahead -- what the countdown counts to."""
        return min(self.open_windows(now), key=lambda w: w.closes, default=None)
