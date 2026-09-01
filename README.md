# TripShield — engine

Cross-provider disruption replanning. Detection is commodity; this is the part
that is not.

`/story` is the whole current capability as one scripted trip: Alex's Singapore
→ Zurich → Milan booking taken through the profile, the meeting, sharing with a
host, a harmless 30-minute delay, a cancellation the agent answers inside the
pre-authorised cap, and a priced-but-not-taken whole-trip cancellation — run
server-side **through the same handlers every button calls**, so it cannot drift
from what a person clicking would get. Scripted inputs, computed outcomes: the
same discipline as `/demo`, applied to the whole product, and re-runnable
because a story that can only be told once breaks at the second audience.
Between test rounds, `maintain.py` clears stored data from wherever
`DATABASE_URL` points (dry-run by default; `--yes` to mean it), and
`POST /api/admin/wipe` does the same over HTTP — but only once
`DOWNSTREAM_ADMIN_TOKEN` is set, and until then it answers 404 like a route
that does not exist. `trips` keeps people and profiles, so shared links
survive and testers simply re-book; `everything` takes the links with it.

`/script` is the demo as a script — seven acts, six minutes, every line to type
in a box and every line to say in italics, with an "if it goes sideways" table.
It reads `/health` first, because the opening instruction depends on whether the
instance is live or replaying. `/test` is the page to send anybody who did not
build this: what the app is, four
things to try with what each should produce, the limits that are deliberate and
not worth filing, and what to put in a bug report. It reads `/health` rather than
asserting anything, because whether an instance is talking to providers or
replaying recordings decides which routes exist at all — and a guide that states
that in prose goes stale silently and sends the whole team hunting the wrong bug.

**One currency inside the engine, and it is Singapore dollars.** Every quote
crosses one boundary — `money.py` — at fixed, declared rates (EUR→S$ 1.50,
USD→S$ 1.30), because the engine sums money and a sum across currencies is a
number in no currency at all. Anything converted says so: `price_source` becomes
`converted` and `quoted` keeps the provider's own words (`EUR 142`). The rates
are guesses and are labelled as guesses, exactly as the rail estimate always
was; EUR is 1.50 rather than the mid-market ~1.45 because the demo's argument is
figures a judge can check by hand, and EUR 282 × 1.5 = S$423 survives mental
arithmetic. The scripted literals cross the same boundary as a live quote does.

```bash
python serve.py                   # the web app, first free port from 8000
python cli.py                     # the same numbers, in a terminal
python cli.py --base written      # the October scenario, as the tests assert it
python mcp_server.py              # the engine as MCP tools, over stdio
python -m pytest tests/ -q        # 226 tests, no keys, no network
```

`/health` reports `ports_mode`, `model_status` and `storage`, and the front page
prints them. `storage` has three states, not two: `postgres`, `memory` because
nobody asked for a database, and `memory` **because one was asked for and is not
answering** — which `storage_status.why_not` names in the driver's own words. A
`DATABASE_URL` that cannot be reached now falls back rather than raising: `store()`
is on the path of every request that touches a trip, so a database that was merely
asleep used to turn the whole app into a 500 rather than into a slightly less
durable one. Losing restarts is recoverable by reading `/health`; losing the app
looks like broken software. `model_status.ready` is the one worth reading: `label` is what was
configured and `ready` is whether it started — a Bedrock key with no model
access says "bedrock:claude-haiku-4-5" in confident blue otherwise, while every
call quietly falls back to a template.

Runs in **replay** by default: every API response is a committed fixture, so
the demo cannot be killed by a cold request or an expired token. To go live:

```bash
export RAPIDAPI_KEY=...  DUFFEL_TOKEN=...        # rail needs no key
DOWNSTREAM_PORTS=record python record.py         # overwrite fixtures for real
DOWNSTREAM_PORTS=live   python cli.py
export LLM_PROVIDER=bedrock                      # or anthropic, or leave unset
```

## What is computed and what is written

Nothing in `domain/ policy/ graph/ plan` contains a severity, a total or a
ranking that a human typed. `S$423`, `-S$21` and `S$264` appear only in
the tests that assert them. That is the whole point: the claim is checkable in
ten seconds by anyone leaning over a laptop.

The model is load-bearing in exactly two places — reading fare prose into
deadlines (`parse.py`) and writing the plan's rationale (`agent.py`). Both have
deterministic fallbacks. Arithmetic does not become unavailable when a token
expires.

## Known limits, stated rather than hidden

- **The rail fare is an estimate.** transport.opendata.ch returns timetables,
  not prices, and SBB publishes no free fare API. Every `Offer` carries
  `price_source`; rail is `"estimate"` and says so everywhere it appears —
  including on the `Booking` once it is selected, which is exactly where it
  starts being added up.
- **Rail is the Swiss timetable and nothing else.** `places.py` names a station
  per city; whether a train runs between two of them is the API's answer, not
  ours, and most pairs come back empty. That is an absence, not a fault, and
  the page says "no trains" rather than raising.
- **Live flight status only covers about a week either side of today**, so a
  fixed October scenario cannot be demonstrated live in September. Anchoring
  the trip relative to run time is scheduled for days 9–10.


## The web app

`serve.py` is a translation layer over the engine and nothing more. Every figure on the page comes
from the same `propagate` and `generate` calls `cli.py` makes — if the page and the terminal ever
disagree, the page is wrong.

Five stages, matching the CLI: the signal, what it costs, the knock-on impact, the ranked plans, and
the handoff. Two things it refuses to hide:

- **`degraded`** — a live call failed and a recording was replayed.
- **`shifted`** — a recording was replayed under a different date because the trip is anchored to
  run time. Both appear in the banner and in `/health`.

Times are formatted server-side in the trip's own timezone. Letting the browser format them renders
a Zurich arrival in the viewer's zone, which is a different flight as far as the traveller is
concerned.

Every row shows **when it starts and when it ends** — the impact cards too, which had a cutoff and
no start, so placing a booking in the trip meant holding the trip in your head. The end carries its
date only when that date differs from the start's: `18 Sep 23:55 → 06:30` reads as a seven-hour hop
that lands before it left, and the arrival is the next morning. Both ends are formatted in their own
zone, so the comparison is between the two dates the traveller actually reads.


## Telling it what you want

`/` is the front door: a sentence, not a form.

    "book me a flight from singapore to london from 1 to 7 september"

`converse.py` is a LangGraph — read, classify, fill, ask, route, search, offer —
and every node except the model call is a pure function of the state. It asks
only what changes the answer: a hotel decides whether LiteAPI is called at all,
and "cheapest, fastest or direct" orders the options now *and* orders the
recovery plans if this trip breaks in a week. Seat preference changes nothing
here, so it is not asked, however conversational it would sound. Every question
carries the reason it is being asked, on screen.

**Trains are searched, never asked about.** Where there is a station at each end
the timetable is called alongside Duffel and the trains appear in the same list
as the flights, ranked by the same preference. It is not a question, because the
same rule decides: whether a train runs between two cities is the timetable's
answer, not the traveller's preference, and "train or plane?" put to somebody
flying Singapore to Bangkok is a question about something that does not exist.
Zurich to Milan under *cheapest* ranks the S$108 trains in the same list as
every fare — first or not by honest arithmetic, since EUR and USD convert at
different rates; under *fastest* a 55-minute flight beats a 3h44 train, and under
*direct* the connecting train correctly sorts below the direct flights.

The chosen leg carries its mode back, because the server re-resolves it and a
train asked of Duffel comes back empty — which reads as a withdrawn fare rather
than as a question put to the wrong shop. **The mode is a hint and the key is
the truth**: given a key the named provider does not have, the other is asked
before anything is declared gone. That is what a client predating trains
produces — it picks the cheapest option, the cheapest option is a train, it
names no mode because it has never heard of one, and the traveller was told an
airline withdrew something no airline ever sold.

Parsing runs deterministically first — cities, dates, party size, the words
"direct" and "cheapest" — and whatever it resolves is final. The model is asked
only about what is still blank, which means a model having an imaginative day
cannot move a date the traveller stated plainly, and the whole thing still runs
with `LLM_PROVIDER=none`.

### Blanks only is half a fence

Filling blanks stops the model *moving* a value and does nothing about it
*supplying* one, and every blank is by definition unfenced. The prompt names
today so relative phrasing can be resolved, which also hands the model a date
to reach for when the sentence has none: asked to read the two words `one way`,
Haiku returns today as the departure and `hotel: false`, every other key
correctly null. It is not being careless — a flight leaves on a day, the only
day in front of it is the one in the prompt — which is why the prompt saying
"do not infer" is not the thing that can stop it.

`hotel` is the expensive one. A hallucinated date at least goes on the card in
full for the traveller to check; a boolean does not appear on the card at all —
it **deletes the question**, and nothing downstream asks again. Answering "one
way" left the traveller with no room and no memory of having refused one, which
is the one thing this file says it will never do.

So a supplied value is corroborated: `mentions_a_day` and `mentions_a_stay` ask
whether the traveller brought the subject up at all, and a value about a
subject they did not raise is dropped. This is not parsing — "next Thursday" is
a cue and not a date, and reading it is exactly what the model is for. It only
has to be a cue somebody actually typed. Two things the cue cannot catch are
caught by arithmetic instead: a return before the outbound is the anchor date
with a couple of nights added to it, and accepting a return date no longer
flips a stated `one way` back to False as a side effect.

The page names the providers it called. Book as many itineraries as you like,
then break any one of them from the panel and take a plan.

### Late, or not going

Two buttons, because they are two different events and the engine turns on the
difference: a delay eventually puts the traveller at the destination, a
cancellation leaves them where they started. **Cancel** and **Late** sit
together on every flight, with the amount beside them — 30m to 12h, spread so
the answer actually changes. Under an hour is absorbed by any sane connection,
a few hours is where a trip comes apart, overnight is a cancellation in all but
name; one hard-coded number would only ever demonstrate whichever of those the
inventory happened to land on.

Delay was the disruption the engine modelled best and the one nobody could
produce. It reached `/demo` from a recorded status and stopped there, so on a
trip you had booked yourself the only available signal was the blunt one. Three
things had to be right before the button could be honest:

- **The clock starts when they are told, not when they land.** `new_end` is the
  moment of learning for a cancellation and the new *arrival* for a delay.
  Taking it as "now" puts the engine hours past deadlines that have not
  happened yet, and a trip with everything still to play for reports nothing
  left to save. `flow.learned_at` bounds it by the departure — you find out at
  the gate at the latest.
- **The walk is ordered by deadline, not by start.** `exposed_to` already
  argued that downstream is a question about deadlines and then handed the
  bookings over in start order. A room whose desk opens at 14:00 came before
  the 15:10 flight that delivers the traveller to it, so a thirty-minute delay
  — comfortably absorbed by a six-hour connection — reported S$668 of room at
  risk. Crying wolf on the commonest disruption is how an alert gets ignored.
- **A delayed leg stays on the itinerary.** `act.apply` dropped the disrupted
  booking unconditionally, which is right for a flight that does not exist any
  more and hands the traveller a trip missing the leg they are about to board
  when it is merely late.

**The row says what happened to it.** `starts` and `ends` are what was *booked*
and they do not move — a delay is what the world is doing to the schedule, not a
correction of it, and overwriting the arrival with a predicted one erases the
comparison the traveller is making. So the affected booking carries a
`disruption` of its own and every other row carries none: a `delayed 3h 15m` tag,
and `now in at 11:30 — was 08:15` underneath. It keeps its colour, because a
delayed leg is still flying and the traveller is still on it; only a cancelled
one is struck through. Until this, `/api/itineraries` knew a trip was disrupted
and not what had happened to it, so a flight running three hours late showed its
original times with nothing to say so.

**And it survives a refresh.** The label was drawn from the last disruption
response, which is a fact about one browser tab rather than about the trip — so
it vanished on reload while the disruption sat in storage the whole time. The
row now reads what happened to *it*, the disruption endpoints hand back the same
rows `/api/itineraries` does, and `/book` keeps the trip id in the address bar.
The trip was always stored; the only thing the page could not do was come back
to one, and a URL is where that belongs — it survives a refresh, a restart, and
being sent to somebody else.

### What replay cannot check

Replay is what makes the demo survivable, and it has exactly one blind spot: in
replay `call` returns the recording and never invokes the lambda that builds a
URL. So every port shipped its own query-string interpolation, none of it was
ever executed by a test, and the first city with a space in it took a live
search down — `InvalidURL` on `cityName=New York`.

Three ports, three degrees of wrong: hotels interpolated raw, rail hand-rolled
`.replace(' ', '%20')` (survives a space, mangles `Zürich HB` — the more
dangerous kind, because it does not raise), and status stripped spaces from a
flight number, which is correct for that field and encodes nothing else.
`base.url_for` is now the one place that knows how to build a URL, and
`tests/test_urls.py` reaches past `call` to assert on the URLs the ports would
actually request.

### Why a selection is matched by key and not by id

Duffel mints offer ids per *offer request*: search twice for the same flight
and you get two ids. That is correct on their side — an id names a priced,
time-limited contract, not an aircraft — and it means an id cannot identify
"the thing the traveller pointed at ninety seconds ago".

Matching on it worked perfectly against recordings, where the fixture returns
the same ids forever, and failed on **every** selection the moment the ports
went live. `Offer.key` is a designator, two airports and a departure minute,
which survives re-searching. Deliberately no fare: the same aircraft is sold
under several brands, so a key can match more than one offer, and `choose`
takes the one nearest the price that was on screen and **reports the
difference** rather than swallowing it. A flight that is gone entirely is
refused — silently booking the nearest thing is how somebody ends up holding a
ticket they did not choose.

## Confirming what was recorded

Nothing is searched, and nothing is filed, until the traveller has looked at
what the system heard and agreed with it. Dates and airports are the two things
a sentence gets wrong most often and the two a person can check in one second —
and every fare, deadline and clash after that point is computed from them.

The card spells dates out in full (`Friday 18 September`, because `18/09` and
`09/18` are the same six characters and different days) and names the codes it
resolved. For an appointment it also names **the city the time is in**.

Saying **no** to it is an answer, not a failure to be understood. It settles
nothing, so the question stays open — but reporting the page's own "no, let me
change it" button as *"Sorry, I couldn't make that out"* told the traveller
their own button was gibberish.

A correction is not a new sentence. Collecting is additive, so "make it the
3rd" cannot wipe the destination; but at the confirmation the only reason to
type is to change something, so a stated field overwrites. `actually the 19th
to the 22nd` works — a bare day is read against the month already on the card,
while a bare number is not, because "2 adults" and "2 nights" are far commoner
than "the 2nd".

Which mode it is in turns on whether the card has actually been **shown**, and
that is recorded when it goes up rather than inferred afterwards. Inferring it
from "is `confirm` the only question left?" was wrong by exactly one turn: the
sentence that *answers* the last question also satisfies that test, so it was
re-read as a correction — and in a fresh parse a lone date is a departure.
Typing `22 september` at "which day are you coming back?" moved the day out to
the 22nd and put the trip home on the same date. Answering a question and
amending an answer look identical from the outside; only the conversation knows
which one just happened.

## Appointments

The same box takes things nobody sold you:

    "meeting with the Milan team on 19 September at 10am at their office"

`appointment.py` chases exactly three things — **who, when, where** — because
those are the three that decide anything. The day and time place it against an
itinerary, the location decides whether it can be reached, and who it is with
is what makes a clash legible when it is read back. Nothing about those three
is ever guessed: a meeting whose time we invented is worse than no meeting in
the system at all, because it will be checked against flights and pronounced
feasible.

**An appointment becomes a `Booking`.** It is a place, a time and a consequence
for not being there, which is what every other row already is — so it is one,
with no price and `commitment=True`, and from that moment the whole engine
applies. `propagate` marks it broken when the traveller cannot get there;
recovery plans weigh options that save it; the deadline watch counts down to it.
Modelling it separately would have meant a second reachability check and a
second clash rule, and the second one is always the one that rots.

`commitment` is the only field that had to be added, and it exists because the
engine reads value from `price`. A S$0 dinner with free cancellation costs
nothing to miss. A S$0 meeting with the Milan team is the reason the trip
exists. Missed commitments are **counted, never priced** — inventing a euro
figure so the meeting could join the money total would be the engine making up
the most important number on the page.

The **room gets its own dates**, asked with the whole trip offered as one tap.
Arriving on the 18th does not mean checking in on the 18th: a red-eye lands at
06:00 and the room is wanted from the night before, and a traveller staying
with family for two nights wants three of the five. Deriving it from the
flights is right often enough to be trusted and wrong quietly. A single date is
refused — half an answer stored is a checkout nobody chose.

**The location is not colour, it is the clock.** "9am at the Ritz Carlton"
means nine in the morning in New York, so an address the engine cannot place is
a question rather than a note: *"Which city is the ritz carlton in?"*, offered
with the cities from the trips that cover that day. Until it is answered there
is no timezone, and without a timezone nothing can be checked.

**A meeting is on the clock of the place it happens.** Taking the timezone of
the trip's first booking is wrong the moment a trip crosses zones: a 09:00
meeting in New York, on an itinerary starting in Singapore, was stamped
09:00 +08:00 and became 21:00 the previous evening — twelve hours out, and an
hour before the flight it was then reported to clash with. `zone_for` prefers
the place's own zone, then the arrival zone of wherever the traveller is that
day, and only then the first booking.

**An address the engine has never heard of is not a place it cannot reach.**
"No route from JFK to ritz carlton" reads as *you cannot get there* and means
*I do not know where that is*; turning the second into the first invents a
clash out of our own ignorance. Unrecognised locations come back as a `note` —
the timing is still checked, the journey is not, and the page says which.

`builder.clashes` then answers two questions: what overlaps, and what there is
no time to reach. A hotel is excluded from both — a room is somewhere you *may*
be, not somewhere you *must* be, and treating a three-night stay as a three-day
commitment makes every meeting in the trip a clash.

## Acting on a plan

`act.py` is the part that makes it adaptation rather than notification.

- **AUTO** is performed. The message is sent, and the result records which
  channels actually took it — possibly none, which is shown rather than hidden.
- **TAP** and **CALL** are handed over with a link and marked pending. Nothing
  is bought: Duffel's test mode cannot take money and we would not use it if it
  could.
- **The itinerary is rewritten.** The cancelled leg goes, the replacement
  arrives flagged `pending`, and that flag survives storage — a real booking in
  the plan and an intention in the record are not allowed to look alike.

## Approve, reject, per line

"Take this plan" was all or nothing. The brief's queue is *approve, reject or
modify*, and the useful case is the middle one: *email the hotel and cancel the
transfer, but I'll sort the flight myself*. Every action now has an `id` — verb
and booking, unique within a plan and stable across re-searching, which a label
carrying a fare is not — and `/api/act` takes a `reject` list of them. Approve
is the default, so an older client that sends nothing takes the plan whole.

A declined action is **recorded as declined, not dropped**: "I chose not to
email the hotel" is a fact the history has to hold, because the room that then
goes unheld is that choice's consequence. The monitor cannot be declined — it
moves nothing and costs nothing, and a plan taken without it is a plan nobody
is watching.

**Declining the replacement keeps the trip open.** The hotel is still emailed,
the cancelled leg still leaves — it is not being flown whatever the traveller
decides — no replacement arrives, and the disruption stays for the watch to
count down. "I will sort the flight myself" is not "nothing happened", and the
page says so. *Modify* is choosing a different row; per-line modification of a
fare or a time is not offered, because the engine would then be pricing a plan
it did not build.

## Buying a trip, then breaking it

`/book` is the same engine with nothing scripted. Search real inventory (Duffel
for flights, LiteAPI for stays, transport.opendata.ch for trains — the first two
on accounts that cannot take money, the third needs no key at all), pick an
outbound, an onward leg and a room, then press **Cancel** or **Late** on any leg
and watch the consequences come out of live data.

**The onward leg offers both modes.** Zurich to Milan is a flight or a train and
the traveller picks; the mode travels with the selection because the server
re-resolves it, and asking Duffel for a train finds nothing and reports the
option as withdrawn. A train is a `Booking` with `Kind.RAIL`, no ticket group —
two flights bought separately are two contracts and that is the scenario's whole
premise, but a rail ticket is not part of that argument either way.

Two things had to exist before a train was reachable rather than merely
offerable. `places.py` had no station to point the timetable at, so nothing
could be searched; and `TRANSIT` had no time between an airport and its city's
main station, so a train that delivered somebody to Milano Centrale left the
Milan hotel — filed under MXP, because that is the code its own search was
pointed at — as far away as another country. `_city` closes the same gap on the
way out: a gap starting at Milano Centrale is a gap starting in Milan, so a
cancelled train is answered by flights from Malpensa as well as by other trains.
That is the cross-provider replanning this README opens by claiming, finally
reachable from a trip somebody assembled rather than from a scripted one. `flow.py` is the whole
loop: `search_flights`, `search_hotels`, `select`, `cancel`, `replan`.

Nothing about the scenario is configured. The recovery query — where the
traveller now stands, where they are contractually due next, by when — is
derived in `plan.recovery_gap`, so cancelling a different leg searches a
different route without an edit. What this flow made visible that a literal
trip had hidden:

- **A cancelled flight is not its own replacement.** The cancellation is ours,
  not the airline's, so its seats are still in inventory and the search returns
  them. Filtered by designator, airports and departure minute.
- **The downstream walk is a question about deadlines, not start times.** Walking by
  start time charged a S$1,547 long-haul that landed that morning to the
  cancellation of a S$255 onward hop.
- **A stay is where the hotel is.** Inferring it from the itinerary filed a
  Milan hotel under Zurich the moment the onward leg landed the next morning.
- **One ledger for every plan, inaction included.** A room defusable by a phone
  call was free under "do nothing" and charged in full to every alternative, so
  the ranking compared two different ledgers.
- **A cancellation never settles.** Doing nothing means the itinerary resumes as
  booked once the day is over — true of a delay, and false of a flight that is
  not going, because nothing carries that traveller to the destination
  overnight. Anything downstream landing on a later day came back "reachable
  under this plan": a Milan room guaranteed to 22:00 is 04:00 tomorrow in
  Singapore, and a 23:55 departure settles five minutes after it is cancelled,
  so a whole cancelled outbound reported *nothing downstream is out of reach*.
- **The walk has to walk.** Judging every booking against the disruption point
  alone leaves `transit` as the only way onward, and `transit` knows ground
  routes, not flights — so a trip that flies on past the connection lost
  everything after it. Zurich to Milan to Rome said the flight to Rome was
  safe and the meeting in Rome was missed, in the same table. A leg the
  traveller can still take is now recorded before the next booking is judged.
  It only ever forgives, and it forgives the baseline and every candidate plan
  through the same map. The scripted trip has one destination and nothing
  flying on from it, which is exactly why it never showed.
- **One reachability model, or the ranking is meaningless.** Candidates were
  scored by a one-hop test of their own — ground transit from where the offer
  lands, and nothing else — while the baseline they are ranked against went
  through `propagate`. It could not see the traveller's own surviving legs, so
  a replacement landing in Milan at 13:40 was still charged for the 16:00
  flight to Rome it had just saved: S$210 on every candidate and nothing on
  doing nothing, and the engine recommended inaction over the plan that
  rescued the trip. `plan.build` now walks the same itinerary with the same
  rules, differing only in the one arrival the offer adds — and `_can_board`,
  which decides whether a replacement can be got to at all, asks the same walk
  rather than building a model of its own. That one refused an onward flight
  out of Milan to a traveller whose delayed long-haul still lands them in time
  for the Milan leg they already hold. Refused, not ranked low: `build` returns
  `None`, so the option never appeared, and all the engine offered was losing
  the room.

The old scripted demo — one late flight, eleven bookings, S$423 — still runs
at `/`, from the same fixtures, asserting the same numbers.

## Who else is in

Eight roles in the brief — traveller, spouse, family organiser, executive
assistant, corporate travel manager, finance approver, destination host,
emergency contact — and "each sees and can do only what is necessary".
`roles.py` names the *necessary* first, as six capabilities (`view`, `money`,
`act`, `book`, `abandon`, `share`, plus `rules` so finance can set the cap
without being able to spend), and a role is a name for a set. A host and an
emergency contact share one set; they stay two names because *"I shared this
with my emergency contact"* has to read back as that.

**There is no login, and it is said so.** Identity is a token somebody was
handed: the owner shares a trip with *Priya, assistant* and gets back a link
with a token in it, shown once and never readable back. Whoever holds the link
is Priya. A token in a URL is a key, and a key can be copied — the honest shape
for a product with no accounts, stated rather than dressed up. No token is the
anonymous traveller every request used to be, so nothing that already worked
needs one; a token nobody issued is refused, not quietly treated as anonymous.

**404, then 403.** A person not let in cannot tell the trip exists. A person let
in as a role can do what that role needs and nothing more: finance can raise the
cap and cannot cancel a leg; an organiser can take a plan and cannot call the
whole trip off; a host can see when and where and nothing with a currency on it.

**Money is redacted by key, not by endpoint.** A role without `money` gets the
same payloads with every monetary field blanked — walked recursively — so a new
endpoint cannot leak a price by forgetting to strip it. `MONEY_KEYS` is the one
list to keep current, and a test breaks the trip as the owner and then reads it
as the host through every read there is, hunting for any number that survived.

Every act is recorded with the name that did it: `acted.by: the assistant`,
`abandoned.by: Charles`, `by: the agent`. Profiles are per person. The watch
serves everybody and asks on nobody's behalf.

## Who is asking

Until `profile.py` every preference lived on the trip it was said on: *cheapest*
was answered again on every booking, the spending cap was set again on every
itinerary, and the rules that made the agent autonomous evaporated with the next
trip. A profile is the same three things said once — **how to rank**, **where
trips usually start**, and **what the agent may do unasked** — and applied at
exactly the point the conversation would otherwise have asked.

**Only what the engine uses.** The brief lists loyalty programmes, dietary needs,
mobility, pace; none of those changes what this engine does today, and a profile
field nothing reads is a promise the page cannot keep. Three fields, each applied
somewhere specific and shown when it is.

**Stated, not inferred, and it loses to the sentence.** The profile is applied
*last* in `converse.read` — after the parse and after the model — and only to
blanks: *"one way to Zurich"* from a Singapore home starts in Singapore; *"Zurich
to Milan"* leaves Singapore out of it; *"fly me to Singapore"* asks where from
rather than booking SIN to SIN. The card names every filled slot — `From:
Singapore · from your profile` — because a slot filled without asking is safe
only when the traveller can see that it was. Inferring the home city from past
trips would be the brief's outcome-learning loop, and it is deliberately not
done: a profile that quietly rewrites itself is one the traveller cannot check.

**A trip copies the rules and owns its copy.** New trips start with the profile's
permissions; changing the profile later does not silently change a trip already
under way, and a trip's own rules can be kept *for every trip* with one
checkbox. Stored per owner in its own table, next to the trips.

## What the agent may do on its own

Two questions decide whether anything happens without a click, and until
`permit.py` the engine asked only the first. **Can we** — does the provider expose
a way to do it? That is `plan.Lane`, a fact about the supplier: an email to a
hotel is AUTO, a fare is a TAP into the carrier's checkout, a SWISS cancellation
is a phone call. **May we** — has the traveller allowed it? That is a fact about
the person, and it is the one that makes the word *autonomous* true: nothing
here acts on its own because the software is capable of it, only because
somebody said it could.

Three rules, on the trip: a **spending cap**, a list of things **never** to
choose (`rail`, `ryanair` — matched against what the agent would *pick*, not
what the trip already holds, so "never Booking.com" cannot stop it phoning the
Booking.com hotel you already have), and verbs that **always ask**. The default
is a cap of zero, which reproduces exactly the behaviour before the file existed.
Nothing becomes autonomous by omission.

**What pre-authorised means here, precisely.** This product cannot buy anything,
so it does not mean "we charged your card" and does not pretend to. It means the
agent *takes the decision* the moment a leg breaks: applies the best plan, sends
everything in the AUTO lane, rewrites the itinerary, and leaves the purchase link
waiting. What always needed a person — the tap, the call — still does, and is
reported as waiting rather than done. The difference is the one that matters:
whether a two-hour delay at 02:00 is answered at 02:00 or at 07:30 when somebody
wakes up and presses a button. The record says `by: the agent`, because an action
taken unasked is exactly the kind a person later wants to find in the log.

**The permission tiers** are the cap grown a second threshold, reading the
deck's numbers the only way both do work: **≤ S$300 act** (taken, sends sent),
**S$301–500 auto-hold** — the agent still *decides*: the itinerary is rewritten
and the replacement enters as `pending`, which is what a hold is in a product
that cannot pay — but nothing leaves the building; the sends are held exactly
as the kill switch holds them, and the notification says *taken and held,
nothing sent, review it* — and **above S$500 nothing without a person**. The
boundaries are tested at 299/300/301/499/500/501. A hold ceiling below the cap
is a contradiction, not a configuration — the auto tier wins and the middle
tier is empty. And the shipped default is still 0/0: the tiers are what the
demo persona configures, not the product default, because otherwise you have
shipped something that spends money out of the box.

**The master kill switch** sits inside the same gate rather than beside it:
`Permissions.disarmed`, and `judge()` short-circuits — every action on every
plan `needs approval` with one fixed reason, the cap ignored, `never` included,
because a disarmed machine does not filter the menu, it stops the kitchen. The
reason is fixed on purpose: a switch that explains itself differently per
action invites arguing with it. Read-only reaches the executor too — the AUTO
lane's emails are **held**, handed to the person with zero channels used, since
read-only that still emails hotels is false in exactly the case a judge would
flip the switch to test. Deadline alerts to the traveller keep flowing: a kill
switch that silences your own warnings is a different and worse product. Every
flip is journalled — *who turned the machine off, and when* is the first
question after any incident involving one — and a rules save leaves the switch
where it was, so setting a cap cannot quietly re-arm it. Note what off means:
`auto_limit = 0` is already manual mode for anything that costs money; disarm
is for the rest.

A plan the rules withhold is not silently missing. The page says *not shown, by
your rules: EC 11:33 (you said never rail)*, because a ranking with the train
quietly removed is a ranking the traveller cannot check.

## Goals before money

`generate` ranked by damage first, and a missed commitment carried an exposure
of zero — so the ranking was **money-first and meeting-blind**. A S$180 flight
that saved the board meeting lost to a S$143 one that missed it, and to doing
nothing at S$0; the engine recommended missing the reason the trip existed to
save S$38.

The sort is now lexicographic: **commitments missed, then damage**, then the
stated preference, then arrival. The principle that a meeting is never *priced*
still holds — there is no euro figure for one anywhere in `plan.py`. It is
*ranked on*, which is different, and the cost of keeping it then appears as a
real number next to the cheaper plan that loses it rather than as a weight
somebody typed:

    Recommended because it keeps Meeting with the client; costs S$108 more than
    doing nothing, which would miss it; within your S$300 limit.

Every clause in that sentence is true of the plan and taken from the engine —
which commitments it keeps, what it costs against the cheapest plan that does
not, what the rules say. A missed meeting also produces the one action that is
possible for it: telling the person. The scripted trip has no commitments, so
its ranking and every figure in it are unmoved.

## Where the traveller is, at every moment

`whereabouts.py` derives a location timeline from the itinerary itself: at
home (the profile's home city) before the first leg, in transit during a leg,
at each destination until the next leg departs, home again after the return.
Movement comes only from the trip's own transport legs — the transit table
knows sixteen *local* hops and is never asked city-to-city questions — and a
trip with no return leg leaves the traveller *presumed* at the last
destination, said out loud as "open-ended", never silently believed.

Every appointment is validated against that timeline for location, date and
time:

- **Wrong city is refused outright** — the one exception to "reported rather
  than refused": a Singapore meeting on a day the timeline puts you in Zurich
  is not saved, and the reply names where you actually are. Nothing is refused
  out of ignorance: an unrecognised place degrades to the old behaviour.
- **Two hours of breathing room** — an appointment closer than 2h to any
  neighbouring event in the same city (a landing, another meeting; the gap to
  a flight runs to its check-in close, not wheels-up) is warned about and
  saved only on an explicit "yes, save it". Flight-to-flight connections keep
  their own `MIN_CONNECTION` rule.
- **A question never writes the calendar** — "when is my lunch appointment"
  is answered from the stored bookings; it used to create a second lunch,
  titled with the question.

## The room follows the flight

Booking a hotel together with flights or rail, "the whole trip" used to key
the room search to the *departure* date — and a red-eye departs the 18th and
lands the 19th. Span-derived nights now wait: the stays search runs only once
the traveller picks a flight, from that flight's actual arrival, on the page
and again server-side at booking time (`choose` re-searches the room from the
arrival of the flight it just re-matched, whatever any client sent). A typed
range of nights — "from the 17th", the room wanted the night before — is the
traveller's own decision and is honoured as given, searched immediately.

## Where the trip is thin, before anything breaks

Everything else in the engine speaks after a disruption. `risk.py` reads the
same itinerary with the same arithmetic at *booking* time and says where it
would snap — three kinds of flag, all derived, none predicted, because there is
no weather feed and no delay statistics here and pretending otherwise would be
inventing the one thing this product exists not to invent:

- **connection** — slack thinner than 90 minutes between landing and the next
  leg's last check-in (a judgment constant, named and visible, like
  `MIN_CONNECTION`); meetings reached with minutes to spare flag the same way.
- **unprotected** — consecutive legs on separate tickets inside the same
  travel day (a twelve-hour window; an overnight decouples them): two
  contracts, nobody owes the connection. The scripted scenario's
  whole premise, surfaced *before* it happens — the demo trip's own booking now
  says *"separate purchases … 75 minutes to spare"* on day one. Legs days apart
  are deliberately not flagged: true-but-useless warnings bury the one that
  matters.
- **breakpoint** — the smallest delay on each leg that starts costing money or
  a meeting, found by running the engine's own `propagate` over a ladder of
  hypothetical delays. Not a prediction that it *will* happen; a statement of
  what happens *if*, from the same arithmetic that will price it on the day.
  The demo's onward hop breaks at 30 minutes (the transfer window is the quiet
  S$72); the long-haul absorbs an hour.

The flags ride on the booking response and the itinerary listing, both pages
show *"where this trip is thin"*, and the money stays out of the prose — the
number lives in the `worth` field so role redaction keeps working by key
instead of by parsing sentences.

## The paper trail

Every field the trail needs already existed as data, and nothing wrote it
anywhere: `permit.Verdict` carried the authorization state and its reason,
`act.Done` carried verb, lane, state and channels, `Policy.source` was the
citation, `monitor.Alert` was the triggering deadline. `audit.py` is assembly,
not invention — one event shape (timestamp, actor, type, subject, **citation**,
**authorization state**, **triggering feed** with its replay/live mode),
append-only through the store, memory by default and Postgres when
`DATABASE_URL` is set.

**The write points are decision boundaries, not the functions.** `propagate`
runs once per candidate plan inside `generate` — twenty-odd calls per replan —
so instrumenting the function would write twenty entries for one decision.
The entry lands where a result is *acted on*: `extracted` when a trip enters
the store (citing the fare prose every later decision rests on), `assessed` and
`judged` once per disruption answered (the reachability rule; the computed
rationale and the verdict), `performed` once per action (lane, outcome,
channels, the approval it ran under, `declined by …` when it did not run),
`alerted` once per delivery (the deadline and what was at stake).

**An entry that cannot explain itself cannot be written.** The constructor
refuses an empty timestamp, citation or authorization state — the acceptance
criterion made structural, with a test proving the refusal. Reading the trail
takes the `money` capability, because the citations quote fares and caps: the
decision log is the ledger with reasons attached, and a host who may not see a
price may not read a sentence that names one. `GET /api/trail` is the data,
`GET /api/trail.txt` is plain text a judge can read on the spot, and the
itinerary panel shows the last few entries with a link to the full text.

## Calling the whole thing off

Not going at all is a different question from being disrupted, and `plan.abandon`
is a different function for it. Nothing broke — the traveller changed their mind
— so there is no reachability to walk, no gap to search and no alternative to
rank. Every other plan answers *what instead?*; this one answers *what now?*.
Building a `Disruption` that did not happen so the recovery machinery would run
would have been the engine lying to itself to reach a familiar shape.

What it does share is the part worth sharing: **one action per booking, in the
lane that can actually perform it**, and `act.perform` to run them — because
from there, *who does this and did they* is the same question.

    TripShield handles it
      - Tell Hotel Le Marais Milano you are not coming
    One tap, you authorise
      - Cancel Malpensa to hotel private transfer            +S$72
      - Cancel Bellagio and Lake Como day tour               +S$192
    You will have to call
      - Cancel LX 1608 - Zurich to Milan Malpensa            +S$57

Each lane is earned rather than assigned. A room with nothing left to recover
gets the **email**, because sending somebody to a booking portal to press cancel
on a non-refundable rate is an errand that returns nothing, while telling the
property is both useful and a thing this system can genuinely do. A fare whose
provider has no consumer cancel API gets the **phone**, from the same
`CAPABILITY` table the recovery lanes come from. A booking that was never paid
for is dropped with a note rather than listed as an errand — sending somebody to
a provider that has never heard of them is worse than silence.

**Priced before it is done, and never in one press.** This is the only action in
the product that destroys value on purpose and cannot be undone by pressing it
again, so `POST /api/abandon` without `confirm` returns what it would cost and
sends nothing. In the chat it is priced **into the thread** rather than into the
sidebar the button sits in: every other consequence in this product is argued in
the conversation — what breaks, what it costs, what the options are — and the one
decision that destroys value on purpose should not be the exception that happens
quietly off to one side. What comes back plus what is gone equals what was paid, at *this*
moment: a window open on Tuesday is shut on Friday, and the figure is only true
next to the time it was computed at.

The trip is **kept, marked** rather than deleted. Which refunds were promised and
which calls are still owed is exactly what a traveller comes back for, and a
delete is no more undoable than the cancellation. The watch stops, because a trip
nobody is taking has no deadlines worth counting down to, and `/api/cancel` and
`/api/delay` refuse it for the same reason.

**Kept is not the same as unchanged.** Every row carries the errand that applies
to *it* — `done for you — Tell the Grand Visconti Palace you are not coming`, or
`yours to do — Cancel LH 0346 · the refund beside it` — and a row carrying an errand is
not a booking any more: struck through, tagged, and no buttons on it. The first
version marked the trip and left the rows alone, and the hotel gave it away.
Flights at least had disabled-looking controls; the room is the row with nothing
on it to look wrong, so somebody who had just called the trip off was still
looking at a bed they had been told they had.

## The watch

An engine you have to open is a report. `monitor.py` is the part that speaks
first: `schedule(trip, disruption, now)` returns every moment somebody has to be
told something, at T-60, T-15 and the deadline itself.

**And it asks, not only counts.** For a long time "the watch is running" meant
less than it sounds: the loop counted down deadlines on disruptions a person had
already pressed a button about, and the status feed was consulted when somebody
opened the page and at no other time — so a flight cancelled at 02:00 was found
at 07:30 by whoever looked. `detect()` now asks the feed about every live trip,
hands a finding to exactly the path the buttons use (`_respond`), and if the
traveller's rules allow it the plan is taken before anybody is awake. The record
says `by: the agent`, the panel says *taken for you while you were away*, and one
notification goes out at the moment of finding — `[DETECTED] LH 0346 cancelled —
taken EC 11:33 (within your S$300 limit); still yours: Book EC 11:33` — because
a traveller whose first word about a cancellation is a T-60 reminder three hours
later has been let down by the notifier, not by the engine.

Each trip is asked at most every ten minutes (`DETECT_EVERY`): the feed is a
paid, rate-limited call per flight, and a flight's status does not change
minute to minute. A trip already being watched belongs to the deadline sweep, a
trip that was called off belongs to nobody, and one bad status call ends the
pass for that trip and not for the rest. Live status reaches about a week either
side of today, so in replay a booked trip's flights are never found — correctly;
the tests stub the feed.

An alert exists only where the clock running out takes something away — a fare
window still worth more than it costs to use, or the last moment a replacement
could still land in time. A deadline with nothing behind it is a fact, not an
alert, and stays in the impact graph. Where two deadlines land on the same
booking at the same minute (the museum's S$27 date change and the museum
itself both close at 16:00) the larger loss wins, because one clock deserves one
sentence.

Nothing is queued. The schedule is a pure function of the trip and the clock, so
the same call answers "what is coming" for the page and "what is due" for the
ticker; firing once comes from a watermark stored on the trip, which is what
makes *once* survive a restart or a second worker.

```bash
export DOWNSTREAM_WEBHOOK=https://hooks.slack.com/...   # or any JSON POST endpoint
export DOWNSTREAM_WATCH=0                               # turn the loop off
```

`notify.deliver` returns the channels that actually took the message, and an
empty list is a real answer the page is obliged to show. Email is deliberately
absent: it needs a verified sending domain, and a mailer that lands in spam
looks delivered on this end. The loop runs in-process, which is real while the
service is awake and stops when a free instance sleeps — `/health` says so
rather than leaving it to be discovered.

## Dates

Everything anchors to **today** by default. Live flight status only covers about a week either side
of now, so a trip pinned to 12 October cannot be demonstrated live in September — "as written" is
the special case, reachable with `--base written` or `?base=written`, and it is what the tests
assert against because those figures can be checked by hand.

`--base` takes `today`, `written`, `+3`, `-1`, or a date. Recordings made on a different day are
replayed with every date inside them shifted to match, and each shift is announced in the banner and
in `/health`. When one route has been recorded more than once, a caller that knows which capture it means
names it — the scripted scenario asks for the October one, so its figures do not move when the
trip is anchored to a different week. Everything else takes the recording nearest the day being
asked about, because the shift is the distortion and the smallest one is the least wrong. Note
what the date in a fixture name is: the day the search was *for*, not the day it was captured,
which is why "newest file" justifies nothing. Only a route with no recording at all still
refuses — that is the failure re-recording actually fixes.

## MCP

The engine is a tool other agents can call. Detection is commodity; cross-provider replanning is
not, and an MCP surface is how that becomes somebody else's building block rather than our demo.

```json
{"mcpServers": {"downstream": {
    "command": "python", "args": ["/absolute/path/to/mcp_server.py"]}}}
```

Three tools: `itinerary` (what the trip is, including the two ticket groups that make nobody
responsible for the connection), `impact_of` (what a delay breaks and which deadline falls first),
and `recovery_plans` (ranked options). Two things travel with the output on purpose — every action's
**lane**, because a `call` action has no consumer API and an agent reporting it as done has lied to
a stranded traveller; and every fare's **price_source**, because the Swiss rail number is ours
rather than a quote.
