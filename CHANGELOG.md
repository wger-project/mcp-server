# Changelog

Changes to the wger REST API itself are documented in the backend's release
notes. This file records important changes to *this package*.

## Unreleased

**Requires wger 2.7.** 
 
The 2.7 API renamed and retyped fields this server
writes to, so a single build cannot serve both releases. See below for what
changed at the tool boundary.

* **Breaking (arguments and results):** the body-weight tools go through
  `/api/v2/measurement/` instead of the `/weightentry/` shim, which is what lets
  the unit travel with the reading. `log_body_weight` and
  `update_body_weight_entry` take `weight` (not `weight_kg`) plus an optional
  `unit` of `kg` or `lb`, recorded on the entry itself; omitted, it is the
  profile's weight unit, so passing it explicitly is also one request cheaper.
  `get_body_weight_history` returns each entry as recorded (`weight` + `unit`)
  alongside the same number in kilograms (`weight_kg`), and takes `date_from` /
  `date_to`; both tools also reach the `notes` field, which the shim had no room
  for.

  The old name was wrong in both directions: the shim never took a unit, it read
  the value in whatever the profile said at that moment, so `weight_kg=80` on an
  imperial profile stored 80 lb — and a later profile switch reinterpreted every
  earlier entry. Correcting a value now keeps the unit it was recorded in rather
  than restamping it, and restating a unit preserves the provenance a health
  import left on the entry instead of replacing `extra_data` wholesale.

  Entry ids are UUIDs, as they already were through the shim — the `WeightEntry`
  table was merged into body-weight measurements and its integer ids went with
  it, so an id held from before wger 2.7 no longer resolves.
* **Breaking (arguments):** `log_workout_session` and `update_workout_session`
  take `started_at` / `ended_at` instead of `when` / `time_start` / `time_end`.
  wger 2.7 stores a session as two timestamps rather than a date plus two wall
  times, so a session may now run past midnight. Both arguments accept a full
  timestamp or a bare date, which lands at 12:00 like everywhere else. Passing
  only `ended_at` is no longer an error: a session without an end is one that is
  still running, and closing it is a patch.
* **Breaking (arguments):** `list_workout_sessions` takes `date_from` /
  `date_to` instead of `when`. 2.7 can filter sessions over a range, so the
  single-day restriction the old argument existed for is gone; pass the same day
  twice for one day. The range is cut on the day a session *started*.
* **Fixed:** patching a measurement no longer rewrites its `source`. The
  generated client fills that field in with `user` unless it is explicitly
  unset, so editing the note on an entry imported from Apple Health or Health
  Connect would have claimed it as hand-entered — and an otherwise empty patch
  would have been sent instead of refused.
* New tool `summarize_measurements`: the server condenses a series into
  per-period rows (count, sum, min, max per category, bucket and unit) instead
  of this server paging the entries and the caller trimming them. A year of
  daily weigh-ins is 365 entries through `list_measurements` and twelve rows
  through this. `bucket=auto` picks the finest period that keeps the series
  under `max_points`; buckets are cut in the trainee's own calendar, which wger
  2.7 knows from their profile.
* New tools `log_blood_pressure` and `get_blood_pressure_history`. wger stores a
  reading as two entries in two child categories of a `blood_pressure` group,
  paired by carrying the identical timestamp — reachable through
  `log_measurement` in principle, but only by finding both categories and
  matching timestamps by hand, and a pair that drifts apart by a second stops
  being one reading. Logging also builds the category group on first use, and
  reading pairs the halves back into one row.
* `create_measurement_category` takes `metric_type`, so an assistant can set up
  the typed categories 2.7 introduced (body fat, height, heart rate, steps, …)
  rather than only free-form ones. The unit defaults to the conventional one per
  type. Creating a `blood_pressure` or `sleep` group also creates the child
  categories its readings go into.
* `list_measurements` takes `source`, separating what the trainee typed from
  what a phone's health sync wrote and from what wger calculates itself.
* **Fixed:** measurement values are no longer bounded at 5000 by this server.
  From 2.7 the range depends on the metric type of the category — 0 to 100000
  for a step count, 0 to 1440 minutes for a sleep stage, 20 to 350 kg for a body
  weight — so one number here fitted none of them and rejected a busy day's
  steps before wger ever saw it. Only the column cap is checked now, and wger's
  refusal names the actual range. `0` is accepted too: a rest day really is
  0 steps, and the old bound required more than zero.
* `list_measurement_categories` takes `metric_type`, and its description
  explains the roles a category can have. The list is no longer just the
  free-form categories a trainee invented: 2.7 adds the official body-weight
  category, the blood-pressure and sleep group containers, their component
  children, and the calculated ones. Three of those refuse entries or deletion,
  which an assistant could previously only discover by being refused.
* Sessions no longer claim that wger allows only one per routine per date. 2.7
  dropped that constraint, and repeating it steered assistants into patching an
  existing session when a second one was wanted.

## 0.2.0

* `add_exercise_with_sets` returns the created ids, as its docstring always
  said, instead of the full serialised slot, slot-entry and config objects.
  Measured over a real routine build: 29 calls returned 62,878 characters,
  38.7% of every tool result in the session, for what a caller uses as three
  ids. Partial-failure diagnostics (`stage`, `slot_rolled_back`, the API error
  body) are unchanged, since those are the fields a caller actually reads.

* The routine tools split into `routines_read` (9 tools) and `routines_write`
  (16), selectable separately through `MCP_TOOLS`. The authoring half is ~4.2k
  tokens of schema — nearly a quarter of the whole surface — that an agent
  following an existing plan never calls. `routines` stays valid and still
  means both halves, so existing configuration is unaffected; tool names do not
  change either way.
* README documents `MCP_TOOLS` profiles with measured token costs, and says
  what the full surface costs so the trade is visible rather than guessed at.
* `log_set` and `update_workout_log` reach the rest of wger's log fields:
  `reps_unit` (a plank was stored as 60 repetitions, not 60 seconds),
  `rest`, the `*_target` counterparts of reps/weight/rir/rest, `session_id`
  and `next_log_id` for dropset chains. `update_workout_log` additionally
  takes `exercise_id` and the plan linkage, so a set logged against the wrong
  exercise or with no routine can be corrected instead of re-entered.
* New tool group `workout_sessions` (5 tools): wger opened a session for every
  logged set, but its own fields — date, notes, `impression`, start and end
  time — had no tool to read or write them.
* Progressions can be gated on the logs: `set_slot_entry_config` and
  `update_slot_entry_config` take `requirements`, wger's rule list over
  `repetitions`, `weight`, `rir` and `rest`. Without it every progression fired
  on schedule whether or not the trainee earned it.
* Slot entries take `entry_type` (warmup, dropset, myo, …) and the
  `repetition_rounding` / `weight_rounding` fields, so a warmup is no longer
  planned as a working set and a percentage step no longer prescribes weights
  no bar can hold.
* RiR arguments now match wger's own rule — half steps up to 4.5
  (`RIR_OPTIONS`), on logs, on `add_exercise_with_sets` and on the `rir` /
  `max_rir` configs. The previous bound of 10 let through values wger's model
  validator rejects, so the caller spent a round trip on a 400 instead of
  being told which values exist.
* Routines take `is_template` / `is_public`, days take `need_logs_to_advance`,
  and `update_slot` / `update_slot_entry` can move a slot to another day or an
  entry to another slot.
* `add_exercise_with_sets` deletes the slot it created when the exercise cannot
  be attached to it. Such a slot holds no exercise, renders in no plan view and
  is listed by no routine tool, so a caller could not find it to clean it up. If
  the cleanup delete fails as well, the response still carries the slot id and
  reports `slot_rolled_back: false`.
* `log_set` can attach a set to the plan it was performed from, via the new
  `routine_id`, `slot_entry_id` and `iteration` arguments. Logs written without
  them are freestanding: wger reads a routine's log view and its statistics
  through that link, so an unattached set is invisible there and in the apps
  that show a plan's progress.
* New tool `get_workout_for_date`: what a routine prescribes on a given date,
  one entry per planned set, with the exercise name and the ids `log_set`
  needs. Reading the same thing by walking days, slots, entries and configs
  costs dozens of requests.

## 0.1.0

* First release on PyPI: `pip install wger-mcp` / `uvx wger-mcp` instead of needing a git checkout.
