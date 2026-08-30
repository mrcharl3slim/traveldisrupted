"""Who the traveller is, kept across trips -- and applied only where it is
visible.

Every test here is about one boundary: a profile fills what the sentence did
not say and never argues with what it did. Cross that in either direction and
the card stops being something a traveller can check.
"""

from __future__ import annotations

import converse
import permit
import profile as profile_mod
import request as rq
from datetime import date

TODAY = date(2026, 8, 29)
HOME = profile_mod.Profile(preference="cheapest", home="SIN",
                           permissions=permit.Permissions(auto_limit=300, never=("rail",)))


def turn(text: str, profile=HOME):
    return converse.turn(text, None, model=None, today=TODAY, profile=profile)["req"]


def test_a_blank_is_filled_from_the_profile_and_marked():
    req = turn("one way to zurich on 18 september, no hotel")
    assert req.origin == "SIN" and req.preference == "cheapest"
    assert set(req.from_profile) == {"origin", "preference"}
    notes = {row["label"]: row.get("note", "") for row in req.card()}
    assert notes["From"].endswith("from your profile")
    assert notes["Sort by"] == "from your profile"


def test_what_the_sentence_says_beats_the_profile():
    """"Zurich to Milan" from somebody whose home is Singapore leaves
    Singapore out of it. A profile is a standing answer to a question, not an
    argument with the traveller."""
    req = turn("one way zurich to milan on 18 september, no hotel, fastest")
    assert req.origin == "ZRH" and req.preference == "fastest"
    assert req.from_profile == ()
    assert all("profile" not in row.get("note", "") for row in req.card())


def test_home_is_not_used_as_an_origin_for_a_trip_going_home():
    """"Fly me to Singapore" from a Singapore home must ask where from rather
    than book SIN to SIN."""
    req = turn("one way to singapore on 18 september, no hotel")
    assert req.destination == "SIN" and req.origin == ""
    assert "origin" not in req.from_profile


def test_a_profile_value_survives_the_next_turn():
    """The merge is field-agnostic and drops bookkeeping it does not know
    about; the marker has to be on the keep list or the card forgets where
    the value came from one turn later."""
    first = turn("one way to zurich on 18 september, no hotel")
    second = converse.turn("cheapest", first, model=None, today=TODAY, profile=HOME)["req"]
    assert second.origin == "SIN" and "origin" in second.from_profile


def test_an_empty_profile_changes_nothing():
    plain = turn("one way to zurich on 18 september", profile=profile_mod.Profile())
    none = turn("one way to zurich on 18 september", profile=None)
    assert plain.origin == "" == none.origin
    assert plain.from_profile == () == none.from_profile
    assert profile_mod.Profile().empty


def test_a_profile_read_from_storage_is_tolerant():
    """An owner with no profile is the empty one; a profile with a place it
    cannot resolve or a preference it does not offer drops that field rather
    than the whole thing."""
    assert profile_mod.Profile.from_dict(None) == profile_mod.Profile()
    odd = profile_mod.Profile.from_dict({"preference": "Comfiest", "home": "Narnia",
                                         "permissions": {"auto_limit": "many"}})
    assert odd == profile_mod.Profile()
    back = profile_mod.Profile.from_dict(HOME.to_dict())
    assert back == HOME
