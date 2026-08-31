"""Clear stored data, from wherever DATABASE_URL points.

    DATABASE_URL=postgres://... python maintain.py trips --yes
    DATABASE_URL=postgres://... python maintain.py everything --yes

Run it with Render's EXTERNAL connection string (dashboard -> the database ->
Connect -> External). Without --yes it only counts and prints -- the delete
needs the word, because a wipe you can trigger by tab-completing the wrong
history entry is a wipe that eventually happens by accident.

`trips` clears itineraries only: shared links (people) and profiles survive,
so testers keep their identities and rules and simply re-book. `everything`
clears people and profiles too -- every shared link dies with it, and whoever
holds one sees "that link is not one I handed out", which is the truth.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import store  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    what = next((a for a in args if not a.startswith("-")), "trips")
    if what not in ("trips", "everything"):
        print(__doc__)
        return 2

    live = store.store()
    status = store.status()
    print(f"storage: {status['using']}"
          + (f"  ({status['why_not']})" if status["why_not"] else ""))
    if not live.durable:
        print("this is the in-memory store of THIS process — a deployed "
              "instance keeps its own. Point DATABASE_URL at the database "
              "you mean, or just restart the instance: memory forgets by itself.")

    trips = len(live.live(limit=1000))
    print(f"would delete: {trips} trips"
          + (", every person and every profile" if what == "everything" else
             " (people and profiles kept — testers re-book, links keep working)"))

    if "--yes" not in args:
        print("nothing deleted. Add --yes to mean it.")
        return 1

    gone = live.wipe(trips=True,
                     people=(what == "everything"),
                     profiles=(what == "everything"))
    print(f"deleted: {gone['trips']} trips, {gone['people']} people, "
          f"{gone['profiles']} profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
