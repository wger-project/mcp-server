# Changelog

Changes to the wger REST API itself are documented in the backend's release
notes. This file records important changes to *this package*.

## Unreleased

**Requires wger 2.7.** 
 
The 2.7 API renamed and retyped fields this server
writes to, so a single build cannot serve both releases. See below for what
changed at the tool boundary.

* The server reads wger's version once at startup and warns when it is older
  than the API client expects, naming both. A warning rather than a refusal:
  most of the surface does not care which release it talks to — the exercise
  and ingredient catalogs have been stable for years — so an operator who
  mostly wants those is better served by a server that starts. What breaks
  breaks visibly. The expected version is not written down: it is the major
  and minor of the installed `wger-api-client`, whose own README states the
  rule — `2.6.x` targets a 2.6 server, the patch component belongs to the
  package. So the two cannot drift, and upgrading the client raises the floor
  with it. wger's own clients already do this — the app pins a
  `MIN_SERVER_VERSION` and the sync command compares before it starts — and
  this server was the one client that did not. Against 2.6 the failure was
  otherwise unreadable: a TypeError from inside the generated client on a
  session write, a valid weight id refused as malformed, a date filter dropped
  without a word. A wger that is unreachable or reports an unparseable version
  is logged and allowed through, since whether it happens to be up at boot
  says nothing about compatibility. A pre-release counts as its release, so
  2.7.0a2 is accepted.
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
* **Security:** an OIDC bearer token must now carry a non-empty string `sub`
  claim, or the request is rejected with 401. Previously a validly signed token
  without `sub` was accepted and its identity fell back to the username claim,
  or to the constant `unknown` when that was absent too. The outbound wger
  credential is cached per identity, so two such callers would have shared one
  cache entry, and the second would have acted with the first caller's wger
  JWT. Common identity providers always put `sub` in access tokens, so a
  typical deployment was not affected; the gap depended on an IdP that omits
  it, together with the default empty allowlist. The username claim still
  serves the allowlist and display, but never keys the credential cache.
  Reported privately by WinstonRedGuard (github.com/WRG-11).

* **Fixed:** a wger that answers too slowly is no longer reported as
  unreachable, and a transport failure always names its reason. httpx raises
  most of these with an empty message — `str()` on a ReadTimeout is `''` — so
  the detail read `wger is unreachable: ` with nothing after the colon: true
  about neither the cause nor the server, since the request had in fact
  arrived. A read timeout now says wger did not answer within the 20s budget
  and points at a slow query; a connect timeout says the connection itself was
  never accepted; and anything else falls back to the exception's type rather
  than to an empty string. The timeout is a named constant so the message
  quotes the budget actually in force.

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

* New inbound auth strategy `MCP_AUTH=wger_oidc` for **wger >= 2.7**, which is
  itself an OAuth2/OIDC provider whose REST API accepts the tokens it issues.
  The server becomes a plain resource server: the caller's wger token goes back
  out on the API call unchanged. No identity provider, no client credentials, no
  token exchange — `WGER_BASE_URL` is the whole configuration. Because the login
  runs in the user's browser on wger's own login page, **MFA enrolled in wger
  works**, which the token-exchange mode could never support. See
  [ADR 0005](docs/adr/0005-native-wger-oidc.md).

  `MCP_AUTH` still defaults to `oidc`, so nothing changes for an existing
  deployment; wger >= 2.7 deployments should set `wger_oidc` explicitly.

* The authorization-server facade serves both OAuth modes, and under `wger_oidc`
  it adds wger's `api:read`/`api:write` scopes to the `/authorize` query and to
  a proxied dynamic client registration. A generic MCP client asks for `openid`
  alone, which wger then refuses with `invalid_scope` — a dead connector with no
  diagnosable cause. `/register` is advertised and proxied exactly when wger has
  dynamic client registration switched on. It also drops the RFC 8707
  `resource` parameter from `/authorize` and `/token`: MCP clients name this
  server there, and wger refuses a token bound to it on every API call. New
  settings: `MCP_WGER_SCOPES`, `MCP_AS_FACADE`, `OAUTH_REGISTER_PATH`.

* Tool errors distinguish the two upstream failures a retry cannot fix. A
  rejected token now carries a hint that the connection has to be authorized
  again, and a missing scope names the scope — an agent that saw only the status
  code retried until it gave up, and a user who had granted `api:read` alone
  watched every write tool fail opaquely. Both are recognised by wger's error
  body, since wger answers an expired or revoked token with `403
  token_not_valid`, not `401`.
* **Fix:** the HTTP transport runs stateless. In a stateful MCP session every
  tool call ran with the identity of the request that opened the session, so
  under `oidc` a caller could act on the session opener's wger account with the
  session id and any valid token of their own, and a refreshed token was never
  used. Clients no longer receive an `mcp-session-id`; nothing on this server
  needed one.
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
  `MCP_OIDC_ALGORITHMS`, `MCP_OIDC_ALLOWED_USERS` and `MCP_WGER_SCOPES`; the
  JSON spelling keeps working.

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
