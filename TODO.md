# TODO

## Release

`dhis2/synthetic-databases` builds the databases with dbgen, upgrades them to each later DHIS2
version and publishes their dumps. dbgen builds at one DHIS2 image, the base its `lock.toml` pins,
and knows no other version: no upgrades, no databases, no S3, no schedule. A move of the base
replaces the table layout in a dbgen release, as dbgen never writes two.

* Build district EX0201 of `examples/pregnancy/` at the base in CI, about 2 minutes, beside the
  checks and tests it runs. No nightly: the pipeline's nightly upgrade follows master.
* Tag v0.1.0, and pin it in the pipeline rather than a commit of `teleivo/dhis2-db-generator`. Its
  dependency `dbgen` becomes `synthetic-db-generator`.

## Next

In order:

* `generate` of a facility's area, such as `dbgen validate --orgunit QP0602-02-05` for 6,513
  events, ran over 30 minutes on 5 cores and 6 GiB before it was stopped, where a ward's takes
  seconds. The area holds every org unit down to its level, here all 28,564 facilities: find the
  step that scales with them rather than with the area.
* Tell builds of one area apart locally. The build name covers only the area, so a build with
  another seed, window, dbgen commit or spec writes to the same `output/stage/<build>/` and
  `output/<build>/`, and replaces the previous one.
* Once the pipeline decides the runner and the sizes, fit the build to them. What pays depends on
  the runner's cores and memory: on 4 vCPUs more streams or threads gain nothing, on 16 cores they
  are the main gain.
  * DuckDB's threads and memory, of `generate` and `load`, set from the `DBGEN_` caps or from the
    machine, as the stack's caps are. `load` splits a table into streams only for `[load] order =
    "parallel"`; `created`, as MNCH loads, takes one stream, so measure what that costs on a
    region first.
  * Dump the events in key ranges, for a region and larger. Nearly all of `dump` is `pg_dump`'s
    one `COPY` of `trackerevent`, printing the `jsonb`, and `pg_dump` sends a table through one
    connection, with `-Fd --jobs` too. On an earlier generator's builds, `dump` took 43 s for a
    district and 20 minutes for 49M events, and four `COPY`s of `trackerevent` by `eventid` range
    took a third of the time of one. Assemble the plain SQL the harness restores from `pg_dump
    --section=pre-data`, the other tables' data, the ranges in one `COPY ... FROM stdin` block and
    `--section=post-data`, each its own gzip member: `gunzip -c` reads concatenated members as one
    stream. The `docker compose exec` pipe costs too: one pipe per range spreads it over cores.
  * Overlap stages where cores and memory are to spare: `build` runs them in turn, but
    `bootstrap` and `generate` do not depend on each other, and `dump` could run beside `verify`.
    Take the dump's snapshot before DHIS2 starts, keep the dump only if `verify` passes, and test
    first that DHIS2's startup takes no lock that waits for `pg_dump`, which holds `access share`
    on every table until it ends.
  * For the largest sizes, drop the unique constraints of the loaded tables during `load` too,
    such as `unique_trackerevent_uid`. `load` drops only the indexes no constraint owns, so every
    event inserts its UID, random to its id, into a B-tree of several GB. Adding the constraint
    back builds the index in one sort and checks uniqueness all the same.
  * Pull the images while `uv sync` runs.
* The import stream, once dumps are published. TrackerTest imports Synthea's women and children,
  whole histories without UIDs, as admin, in a loop: nothing continues the database, while in a
  live instance most writes do.
  * dbgen draws the future already and drops it: `generate.py` keeps an enrollment's events before
    `window_end`, the first after it as SCHEDULE, and drops enrollments that start after it. A
    horizon past `build.end` puts what falls in it into a stream instead: the next events of the
    dump's enrollments, its SCHEDULE events completed or skipped, enrollments closed, later
    enrollments, new registrations keyed after the dump's so the dump stays the same, children
    with their relationship to their mother, and `records.updated` past the end.
  * one line per submission, what a clerk saves at once: flat tracker JSON with dbgen's UIDs, its
    time and its clerk, without the fields rules assign from today (`unvalidated`), which the
    importer's rules fill
  * size the horizon by the runs: a load run imports about 30,000 entities a program, a month or
    more of a district's records
  * decide `now`: MNCH's is two months after `build.end` with no records between, so a stream from
    `build.end` starts before the dump's today. DHIS2 stamps `createdAt` with the real date, and
    the gap from the dump's `now` grows every year a dataset lives: re-anchor the window at a
    dataset release.
  * imports by duration need data that never ends: new registrations without UIDs, nested as
    Synthea's, looped, in place of the Synthea files
* Before the pipeline's first release, check that the planner misestimates the tracker queries on
  a build as on a real instance (their q-errors). The `stats` stage, the collector and `dbgen
  report` did, on the branch `backup/planner-report` of `teleivo/dhis2-db-generator`.

## Ideas

### Reproduce an implementation's performance problem

An implementation reports a slow request. They can give us their metadata but not their database,
which holds patients. What else would we need to write a spec whose build shows the same problem,
the same slow plan, so we can find its cause, test a fix and keep the database as a perf test?
dbgen would then make not just a realistic database, but one of their shape without their data.

The branch `backup/planner-report` of `teleivo/dhis2-db-generator` did part of it, to calibrate
the MNCH spec rather than to chase an implementation's problem:

* `src/dbgen/sql/collector.sql`: one SQL script a database admin runs with `psql` on a restored
  copy. It writes nothing (temporary functions, a transaction rolled back) and outputs JSON of
  numbers: per program its records and their ratios by stage and how its events spread over org
  units; the `pg_stats` of the columns tracker filters by, as counts and fractions, never values;
  and per query of a fixed set, at the busiest, the median and the quietest org unit, the plan as
  a tree of node types, tables, estimated and actual rows, loops, times and buffers. It chooses
  the parameters inside the database by rank (org units by their events, a common and a rare name
  by their count), so nobody reads a real name to pick one. Its header lists where the obvious
  statistics leak values: `most_common_vals` and `histogram_bounds`, the user columns, geometry,
  a plan's text, parameters picked by hand, UIDs.
* `dbgen capture`: sent TrackerTest's requests to a build and read the SQL DHIS2 ran from the
  Postgres log, which became the fixed query set (`src/dbgen/queries/`)
* `stats`: ran the collector on every build, searched its output for every value, username, org
  unit and UID of the build and failed on a match, and held the build to a target report
  (`[stats]` `target`, `within`, `skip`) by each plan's worst q-error
* its weak spot, which the branch's TODO weighed: the query set was the SQL of one DHIS2 image,
  captured again at every image bump, so the collector fit only instances on that version. And it
  measured whether the planner is as wrong as on a real instance, not which part of the spec to
  change.

Keep: one read-only script, numbers only, parameters chosen by rank inside the database, the same
script run on their copy and on our build so the two compare, and the leak test on every build.
Leave: the fixed query set. The culprit decides what to measure.

#### What we ask for

* the DHIS2 and Postgres versions, and the hardware
* the metadata of the programs involved: programs, stages, data elements, attributes, option
  sets, tracked entity types, relationship types, program rules. No users, no org unit names.
* the culprit: the request, how often it runs, for whom and how slow. A script masks it: metadata
  UIDs stay, an org unit becomes its level and rank by records, a user their superuser flag and
  the sizes of their capture and search scopes, a filter value its operator, its length and the
  records it matches.
* the profile (see "The profile"), run on a restored copy or a standby
* if they can, the culprit's plan from `explain (analyze, buffers, format json)`, stripped by the
  same script to node types, tables, indexes, estimated and actual rows, loops, buffers, and its
  conditions with the constants masked

#### The profile

What it measures, by the spec key it fills:

* `[orgunits]`: the org units per level, their children per parent as quantiles (`per_parent`),
  the levels programs register at
* the spread of records over org units: records per org unit as quantiles and the busiest's share
  (`tracked_entities`' `by`, the facility traits)
* per program: tracked entities, enrollments per tracked entity and events per enrollment by stage
  as quantiles (`enrollments.count`, `visits.count`, a stage's `share`), the days between them
  (`gap`, `spacing`), the statuses and the shares deleted and followed up (`status`, `[records]`),
  and records per month (`build.start`, `build.end`)
* per data element and attribute: its fill rate (`fill`), each option's share (`weights`: option
  codes are metadata), a number's 1st to 99th percentiles (its distribution), a text's distinct
  values and the counts of its 1st, 10th, 100th and 1000th most common value (`words`' `n`, `s`
  and `q`), never a value
* `[users]`: users per level, the sizes of their scopes, the share of superusers, who skip the
  access checks a query otherwise joins
* the physical side: table and index sizes, the indexes beyond DHIS2's own (one an implementation
  added changes plans), dead tuples and the last analyze, `pg_stats`' correlation (`[load]
  order`), and the planner's settings (`work_mem`, `random_page_cost`, `effective_cache_size`,
  `default_statistics_target`)
* the top statements by total time from `pg_stat_statements`, if installed: normalized, so
  without constants, with their calls, mean time and rows
* for the culprit: the rows each filter of its SQL matches, alone and together, at the culprit's
  parameters. Dependencies between columns are what traits draw and what the planner misjudges,
  and measuring every pair is impossible: the culprit names the few that matter.

Rules, so that its output is no personal data:

* counts, fractions, quantiles, times and the planner's numbers, metadata UIDs and option codes.
  Never a value of a text column, a record's date, a coordinate, a username, a plan's text, or the
  UID of an org unit, user or tracked entity.
* nothing by a single org unit, only by rank, and no count, share or quantile of fewer than k
  records (k = 10?): an option's share in a small facility can name a patient
* numbers as their 1st to 99th percentiles, not their minimum and maximum, which are one person's
* plain selects in one read-only transaction with a `statement_timeout`, and no temporary
  functions, so it runs on a standby too, which refuses them
* JSON a person reads before sending it
* our CI runs it on every build and fails when its output holds a value, username or UID of the
  build, as the branch's `stats` did

#### From profile to spec

* `dbgen draft <metadata> <profile>`: a spec from the metadata and the profile, its counts,
  quantiles and fill rates turned into distributions, each line commented with the number it came
  from. The dependencies the culprit needs are written by hand.
* Build an area at their scale for the programs involved: plans change with absolute sizes, not
  only selectivities (a hash join or a nested loop, an index or a sequential scan).
* Run the profile on the build and compare it with theirs, number by number, and the culprit's
  plan with theirs: the same shape, each node's q-error within a factor (the branch's `[stats]`
  thresholds). Change the spec until they match.
* Then the build shows the problem: find its cause, test the fix, and publish the spec as a
  database of the pipeline, so the perf tests keep it from coming back.

#### Open

* Their metadata will hold what dbgen cannot generate yet, such as event programs, geometry or
  other relationship shapes. `compile` fails on it, so an implementation may need dbgen features
  first.
* An implementation older than the pipeline's base, 2.42.0, can be reproduced only from 2.42 on,
  where the problem may be fixed already.
* The profile reads the schema, which 2.43 changed (`V2_43_21` split `event`), so it must handle
  every version implementations run, or at least the supported ones.
* Where it lives (dbgen, the pipeline, or core beside the perf tests), who runs it with the
  implementation, and under what agreement.
