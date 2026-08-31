"""The paper trail: what was observed, decided, done -- and on whose say-so.

Every field here already existed as data and nothing wrote it anywhere:
permit.Verdict carried the authorization state and its reason, act.Done
carried verb, lane, state and channels, Policy.source was the citation, and
monitor.Alert was the triggering deadline. This module is assembly, not
invention -- one event shape, written at the moments a result becomes a
decision, append-only.

WHY THE WRITE POINTS ARE DECISION BOUNDARIES AND NOT THE FUNCTIONS. The deck
names ingest.extract, graph.propagate, permit.judge, act.perform and the
monitor. propagate runs once per candidate plan inside generate -- twenty-odd
calls per replan -- so instrumenting the function would write twenty entries
for one decision and "one entry per decision" would be false on day one. The
entry is written where the call's result is ACTED ON: extraction when the
trip enters the store, assessment and judgement once per disruption answered,
performance once per action, an alert once per delivery.

AN EVENT THAT CANNOT EXPLAIN ITSELF CANNOT BE WRITTEN. The constructor
refuses an empty timestamp, citation or authorization state. That is the
acceptance criterion made structural: there is no code path that produces an
unexplained entry, because the entry raises first.

APPEND-ONLY. The store exposes append and read and nothing else; the only
deletion is the admin wipe that clears test data wholesale, and that is a
property of the store, stated there.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

#: What kind of decision an entry records.
EXTRACTED = "extracted"    # a trip's bookings and fare rules entered the store
ASSESSED = "assessed"      # a disruption was propagated across the trip
JUDGED = "judged"          # the recommended plan met the traveller's rules
PERFORMED = "performed"    # an action ran, was handed over, or was declined
ALERTED = "alerted"        # a deadline notification left the building

#: Authorization states an entry may carry, beyond permit.py's own four.
OBSERVED = "observed — no authorisation involved"
NOTIFICATION = "auto — notification only"


@dataclass(frozen=True)
class Event:
    at: str            # ISO timestamp, UTC
    actor: str         # who decided: a name and role, "the agent", "the watch"
    type: str          # one of the five above
    subject: str       # what it is about: trip, booking, plan or action
    citation: str      # the policy or rule the decision rests on, quoted
    authorization: str # what the permission engine said, or OBSERVED
    feed: str          # the triggering feed: port name plus replay/live mode

    def __post_init__(self):
        for field_name in ("at", "actor", "type", "subject",
                           "citation", "authorization", "feed"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"an audit event needs a {field_name}; "
                                 "an entry that cannot explain itself "
                                 "cannot be written")


def record(store, trip_id: str, *, actor: str, type: str, subject: str,
           citation: str, authorization: str, feed: str) -> dict:
    """Stamp, validate, append. The timestamp is taken here, not accepted --
    a caller that could supply one could supply a wrong one."""
    event = Event(at=datetime.now(timezone.utc).isoformat(),
                  actor=actor, type=type, subject=subject,
                  citation=citation, authorization=authorization, feed=feed)
    payload = asdict(event)
    store.audit(trip_id, payload)
    return payload


def render(events: list[dict]) -> str:
    """The trail as plain text a judge can read on the spot."""
    lines = []
    for e in events:
        lines.append(f"{e['at']}  [{e['type']:<9}] {e['actor']}")
        lines.append(f"    subject:        {e['subject']}")
        lines.append(f"    citation:       {e['citation']}")
        lines.append(f"    authorization:  {e['authorization']}")
        lines.append(f"    triggering feed: {e['feed']}")
        lines.append("")
    return "\n".join(lines) if lines else "nothing recorded yet\n"
