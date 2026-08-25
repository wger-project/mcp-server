# Changelog

Changes to the wger REST API itself are documented in the backend's release
notes. This file records important changes to *this package*.

## Unreleased

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
* First release on PyPI: `pip install wger-mcp` / `uvx wger-mcp` instead of needing a git checkout.
