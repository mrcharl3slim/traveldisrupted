"""Who the traveller is, kept across trips. Consent-based, and applied.

Until this file every preference lived on the trip it was said on. "Cheapest"
was answered again on every booking, the spending cap was set again on every
itinerary, and the rules that made the agent autonomous evaporated the moment
the next trip was booked. A profile is the same three things said once.

ONLY WHAT THE ENGINE USES. The brief lists loyalty programmes, dietary needs,
mobility, pace; none of those changes what this engine does today, and a
profile field nothing reads is a promise the page cannot keep. What is here is
what is applied: how to rank (`preference`), where trips usually start from
(`home`), and what the agent may do unasked (`permissions`). Each is applied
at the point the trip would otherwise have asked, is shown on the confirmation
card as coming from the profile, and can be overridden by saying so.

STATED, NOT INFERRED. The confirmation card is the whole reason it is safe to
fill a slot from here: the traveller sees "From: Singapore -- from your
profile" before anything is searched, and one word changes it. Inferring a
home city from past trips would be the outcome-learning loop the brief also
asks for, and it is deliberately not done yet -- a profile that quietly
rewrites itself is one the traveller cannot check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import permit
import places
from request import PREFERENCES


@dataclass(frozen=True)
class Profile:
    #: How to rank options: "cheapest", "fastest" or "direct". Empty asks.
    preference: str = ""
    #: Where trips usually start, as a place code. Empty asks.
    home: str = ""
    #: What the agent may do on its own, applied to every new trip. The trip's
    #: own rules still win once set -- a profile is a default, not a policy.
    permissions: permit.Permissions = field(default_factory=permit.Permissions)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Profile":
        """Tolerant, as everything read from storage has to be. An owner with
        no profile yet is the empty one, and the empty one changes nothing."""
        raw = raw or {}
        pref = str(raw.get("preference") or "").lower()
        found = places.find(str(raw.get("home") or ""))
        return cls(
            preference=pref if pref in PREFERENCES else "",
            home=found.code if found else "",
            permissions=permit.Permissions.from_dict(raw.get("permissions")),
        )

    def to_dict(self) -> dict:
        return {"preference": self.preference, "home": self.home,
                "home_label": places.label(self.home) if self.home else "",
                "permissions": self.permissions.to_dict()}

    @property
    def empty(self) -> bool:
        return not (self.preference or self.home or self.permissions.anything
                    or self.permissions.never or self.permissions.always_ask)
