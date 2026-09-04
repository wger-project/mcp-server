# Changelog

Changes to the wger REST API itself are documented in the backend's release
notes. This file records important changes to *this package*.

## Unreleased

* **Fixed:** `get_workout_for_date` no longer fails on a date the routine
  covers but schedules no day on. wger returns one sequence entry per calendar
  day, and `fit_in_week` pads the rest of the week with entries whose `day` is
  null — four of the seven for a three-day split, so the common shape rather
  than an edge case. The tool reached for that day's id and raised. It now
  answers the way it already did for a rest day: `planned: []` with
  `is_rest_day` true, keeping the entry's own iteration.

  The null itself was never in wger's OpenAPI schema, which declares both `day`
  and `label` as required and non-nullable on the two `WorkoutDayData`
  serializers. The generated client believed it and died parsing the response
  before this server saw it, which is why the tool failed for entire routines
  rather than for single dates. Fixing that needs `allow_null=True` on those
  serializer fields and a regenerated client; this change is what the server
  does once such an entry reaches it.

* **Security:** a bad configuration no longer prints part of `WGER_API_KEY` to
  the log. Pydantic attaches the raw input to a `ValidationError` as
  `input_value` and truncates only its middle, so the tail of whatever secret
  was set survived into the message — and nothing caught that error, so it
  reached stderr as an uncaught traceback: the client's MCP log under stdio,
  the container log under http. A short key would have appeared in full.
  `load_settings` now restates such a failure as `ConfigError`, carrying the
  messages and none of the values, and the server turns it into the same
  one-line exit it already gave for a bad `--transport`. Separately,
  `WGER_API_KEY`, `MCP_STATIC_TOKEN` and `OIDC_CLIENT_SECRET` are `SecretStr`,
  so anything that formats the settings object — a log line, a traceback frame
  — sees `**********` rather than the value. Reading them back in code needs
  `.get_secret_value()`; note that `str()` on one of these yields the mask, not
  the secret.

* `log_set`, `add_exercise_with_sets` and `attach_exercise_to_slot` take their
  default weight unit from the trainee's own wger profile instead of leaving a
  hardcoded `kg`. A profile set to pounds now records pounds when the caller
  omits `weight_unit`; before, a trainee reporting "225" had it stored as 225
  kg, wrong by a factor of 2.2 and indistinguishable downstream because the
  number is plausible either way. An explicit `weight_unit` still wins, and a
  profile that cannot be read refuses the write instead of guessing: the guess
  is unrecoverable once stored, while the refusal costs one retry with an
  explicit unit.

* `get_workout_for_date` returns the day's `description` as `day_description`.
  A routine's per-day notes are where rep ranges, machine substitutions and
  form cues live, and the tool that answers "what am I doing today" was
  returning the planned numbers without the terms they were written under — a
  caller reporting the plan quoted a bare rep count where the routine had
  specified a range. Unset descriptions come back as `null`, matching
  `day_name`.

* `attach_exercise_to_slot` and `update_slot_entry` accept a unit NAME for
  `repetition_unit` and `weight_unit` — any of the names `log_set` already took
  — as well as wger's numeric id. Before, these were the only unit fields in the
  server that took a bare integer with no mapping, so a caller had to know that
  seconds is 3; `log_set` has always taken names. A wrong id is invisible
  afterwards: a 30-second hold written with id 2 is stored as "30 until
  failure", and nothing in the record says it was meant to be time. On these two
  tools numeric ids still pass through unchanged, whether sent as a number or as
  a string like `"3"`, and an unknown name is refused before the write instead
  of reaching wger. Elsewhere — `log_set`, `update_workout_log`,
  `set_slot_entry_config`, `add_exercise_with_sets` — the unit parameters are
  typed `str` and have never taken a number, so a number stays refused there.
* Unit names are matched case- and space-insensitively everywhere, so wger's own
  display names ('Seconds', 'Until Failure') are accepted alongside the fixture
  names.
* The unit lists in `log_set`'s docstring and in the README are reordered to
  match wger's actual ids and now say to pass the name rather than infer a
  number from the list's order. As written before, they listed seconds second
  while seconds is id 3 and `until_failure` is id 2, so a reader counting
  positions arrived at exactly the wrong value. The docstrings of the two
  slot-entry tools, which do take an id, now state every id outright.

* `add_exercise_with_sets` takes an optional `max_reps`, so a planned set can
  record a rep RANGE rather than a single number. Without it, "3 x 8-12" had to
  be stored as `8` and the top lived only in the conversation; a trainee whose
  progression rule is "add weight once you beat the top of the range" had no
  stored top to beat. `max_reps` below `reps` is refused before anything is
  written, since `reps` is the bottom of the range. The `max_reps` config kind
  already existed for `set_slot_entry_config`; this reaches it from the
  high-level authoring call.

* **Breaking (response shape):** `add_exercise_with_sets` returns the ids
  flat — `slot_id`, `slot_entry_id`, `sets_config_id`, … — instead of 0.2.0's
  one-key sub-dicts (`{"slot": {"id": ...}}`). The nesting was a vestige of
  the full objects and made every caller map `["slot_entry"]["id"]` onto the
  `slot_entry_id` parameter the follow-up tools actually take; the flat keys
  match those parameter names, and the delete tools' responses, verbatim.
* **Breaking (response shape):** `lookup_food_by_barcode` and
  `lookup_foods_by_barcodes` no longer return `wger_ingredient_payload`. It
  repeated the numbers already in `macros_per_100g` under a second set of keys,
  shaped for a `create_ingredient` call that cannot exist — wger's REST
  `/ingredient/` is read-only, and the tool was removed with the move to
  multi-user auth. Every lookup was paying context for it. The macros
  themselves are unchanged; read them from `macros_per_100g`.
* `lookup_food_by_barcode` retries once on a 429 from Open Food Facts,
  honouring `Retry-After`. The batch variant always did; the two were separate
  implementations of the same request and now share one, so the difference was
  never a decision anyone made.
* Comma-separated values work in an env file, not just in the environment.
  `ALLOWED_HOSTS=a,b` in `.env` used to abort startup with a parse error, since
  only the process environment was rewritten to the JSON form the settings
  loader understands. Affected `ALLOWED_HOSTS`, `MCP_TOOLS`,
  `MCP_OIDC_ALGORITHMS` and `MCP_OIDC_ALLOWED_USERS`; the JSON spelling keeps
  working.

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
