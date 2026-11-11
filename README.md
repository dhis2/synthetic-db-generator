# DHIS2 Synthetic Database Generator

`dbgen` builds synthetic DHIS2 tracker databases for performance testing. It reads a DHIS2
metadata export and a spec, and writes a Postgres dump for the [DHIS2 performance
tests][perf-setup]. The same metadata, spec and seed give the same data (see
[Determinism](#determinism)).

[perf-setup]: https://github.com/dhis2/dhis2-core/tree/master/dhis-2/dhis-test-performance

It builds for DHIS2 2.42.0, the image pinned in `src/dbgen/lock.toml`. The published databases,
such as Perf Land MNCH, are built with dbgen in [synthetic-databases][synthetic-databases], which
upgrades them to each later DHIS2 version and publishes their dumps. Look there for those dumps
and for a realistic spec.

[synthetic-databases]: https://github.com/dhis2/synthetic-databases

## Quick start

The example in `examples/pregnancy/` is a small spec for Example Land, a made-up country of 2
regions, 6 districts and 24 facilities. A woman has one pregnancy or more, each with a profile,
monthly visits of a check and a lab test, and a closing event. The children of live births enroll
in a second program, linked to their mothers, with a birth event and vaccinations.
`pregnancy.toml` explains each choice in a comment.

These commands need no Docker:

```sh
uv run dbgen areas examples/pregnancy/pregnancy.toml --orgunit EX02    # the areas to build
uv run dbgen explain examples/pregnancy/pregnancy.toml                 # how each field is drawn
uv run dbgen sample examples/pregnancy/pregnancy.toml --orgunit EX0201 # its records as Parquet
```

This one builds district EX0201 into a dump in about 2 minutes, and needs Docker (see
[Requirements](#requirements)):

```sh
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
```

## Why

Performance tests need tracker data of a realistic size and shape: few busy facilities and many
quiet ones, names that repeat, records spread over years, values that depend on each other. The
Postgres planner assumes evenly spread, independent columns, so data drawn column by column hides
the misestimates and slow plans that real data can cause (see [How Good Are Query Optimizers,
Really?][leis]).

[leis]: https://www.vldb.org/pvldb/vol9/p204-leis.pdf

The metadata already says what valid data looks like: programs, stages, value types, option sets,
compulsory and unique fields. `dbgen` makes valid data from it with no configuration. The spec
adds what the metadata cannot say: how much, how it is spread, and what depends on what.

## Concepts

* **Metadata**: a DHIS2 metadata export of the tracker programs to fill.
* **Spec**: a TOML file of what the metadata cannot say (see [Write a spec](#write-a-spec)).
* **Org units**: the hierarchy the spec draws, a root and levels below it such as districts and
  facilities. Each org unit has a code, which `dbgen areas` lists.
* **Area**: the org units a build covers, by their codes, each with every org unit below it.
* **Records**: a tracked entity registers at an org unit and has enrollments in a program, each
  with events of the program's stages. A visit is one event of each of several stages, on one day.
* **Trait**: a hidden number of an org unit, tracked entity or enrollment that values depend on
  (see [Traits](#traits)).
* **Clerk** and **officer**: users of a build. A clerk captures the records of an org unit; an
  officer only views those below one.

## Limitations

* Tracker programs only: metadata with an event program fails.
* No record is deleted.
* An event is entered on its day, never later.
* The program rules do not run: the spec must satisfy them (see [Program rules](#program-rules)).

## Build a database

### Requirements

* Linux, [uv](https://docs.astral.sh/uv/) and Docker.
* 17 GiB of free memory: 4 GiB for Postgres, 5 GiB for DHIS2, and 8 GiB for DuckDB and the rest
  of the machine. DHIS2 and Postgres refuse to start without it. On a smaller machine, lower the
  [caps](#caps).
* Disk: about 1.5 GiB per million events for the build database, plus up to 4 GiB of WAL, and
  0.25 GiB per million events for the dump. District QP1613 of [MNCH][mnch], 2.2 million events,
  takes 6.4 GiB and 0.5 GiB. DHIS2 and Postgres refuse to start with less than 10 GiB free, and
  `load` with less than 8 GiB plus 1.5 GiB per million events.

### Choose the area

`dbgen areas` lists an org unit, the root by default, and its children, with their traits and the
tracked entities and events a build of each would hold:

```
$ uv run dbgen areas examples/pregnancy/pregnancy.toml --orgunit EX02
code    level     name       children  people  tracked entities  events
EX02    Region    Pubek             3                     3,610  32,072
EX0201  District  Soyonimax         4  15,373               640   5,682
EX0202  District  Molarofuk         4  44,180             1,838  16,329
EX0203  District  Takot             4  27,221             1,132  10,061
```

The counts are estimates from the spec's means: a build holds fewer.

### Build

`--orgunit` takes the codes of the area; without it, a build covers every org unit. Several codes
need a `--name`:

```sh
# output/pregnancy-EX0201/
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
# output/pregnancy-both/
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201 EX0202 --name both
```

The first command builds 518 tracked entities and 4,055 events in about 2 minutes, into a dump of
half a MiB and its `manifest.json`. The first build also pulls the Docker images pinned in
`src/dbgen/lock.toml`. The database holds every org unit down to the area's level, those below it
only in the area, and the area's users and records.

Every build first empties the build database of its checkout, the Docker volume
`dbgen-<checkout>-<hash>_pgdata`, so run one build at a time per checkout. Builds of separate
checkouts run side by side, as far as memory allows. dbgen prints the host ports of Postgres and
DHIS2 when it starts them.

### Caps

Postgres and DHIS2 run in containers capped by these environment variables. They start only when
their memory caps plus `DBGEN_HEADROOM` are free.

| Variable | Default | Caps |
|---|---|---|
| `DBGEN_DB_MEMORY` | `4g` | Postgres's memory |
| `DBGEN_DB_CPUS` | `4` | Postgres's CPUs, and the streams of a parallel `load` |
| `DBGEN_WEB_MEMORY` | `5g` | DHIS2's container's memory |
| `DBGEN_WEB_CPUS` | `4` | DHIS2's CPUs |
| `DBGEN_WEB_HEAP` | `3g` | DHIS2's Java heap, less than its container's memory |
| `DBGEN_HEADROOM` | `8g` | the memory left free for DuckDB and the rest of the machine |

A build of up to about 100,000 events, such as ward QP0602-02 of [MNCH][mnch], fits a standard
GitHub runner of 4 CPUs and 16 GB with:

```sh
DBGEN_DB_MEMORY=3g DBGEN_WEB_MEMORY=4g DBGEN_WEB_HEAP=2500m DBGEN_HEADROOM=3g \
  uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
```

## Use a dump

A dump is plain SQL. Restore it into an empty database of Postgres 16 with PostGIS, such as the
`postgres` image pinned in `src/dbgen/lock.toml`:

```sh
createdb dhis
gunzip --stdout output/pregnancy-EX0201/dhis2-db-pregnancy-EX0201.sql.gz | psql --dbname dhis
```

Run DHIS2 on it with the image it was built on, `images.dhis2` of its `manifest.json`: another may
migrate the schema at start or fail on it. Core's [performance test setup][perf-setup] restores
its databases from S3 and takes no local dump yet.

The users have the password `Perf-Test-2026!` and the spec's name as prefix, unless `[users]` sets
them. They are for performance testing only:

* `<prefix>.<org unit code>.<n>`: a clerk of an org unit a program registers at
* `<prefix>.<level>.<org unit code>`: a view-only officer of a level of `users.officers`
* `<prefix>.admin`: an admin with the ALL authority
* `admin`, password `district`: DHIS2's default superuser

Usernames are in lower case, such as `pregnancy.ex0201-01.1` and `pregnancy.district.ex0201`.

## Write a spec

Only `[build].metadata` is required. With nothing else, `dbgen` fills every compulsory field with
a value its metadata allows and leaves the others empty (see [What the metadata
gives](#what-the-metadata-gives)). Such values pass the importer's checks but not always the
program rules (see [Program rules](#program-rules)).

### What the metadata gives

| Metadata | Default |
|---|---|
| option set | an option, uniform |
| `BOOLEAN` | `false`, as program rules act on `true` |
| `TRUE_ONLY` | `true` |
| `INTEGER*`, `NUMBER`, `PERCENTAGE`, `UNIT_INTERVAL` | uniform in a range that fits the type |
| `DATE`, `DATETIME` | uniform in the data window |
| `TIME` | uniform in working hours |
| `AGE` | a date of birth, the age exponential of mean 25 years, at most 100 |
| `TEXT`, `LONG_TEXT` | made-up words of syllables |
| `LETTER` | a letter |
| `PHONE_NUMBER` | made up after the spare country code +289 |
| `EMAIL`, `URL` | made up at `example.org` |
| `ORGANISATION_UNIT` | the record's org unit |
| `USERNAME` | the record's clerk |
| `FILE_RESOURCE`, `IMAGE`, `COORDINATE`, `GEOJSON`, `MULTI_TEXT`, `REFERENCE`, `TRACKER_ASSOCIATE` | none, so a compulsory one needs a value in the spec |
| unique attribute, `TextPattern` | from the tracked entity's number, so never repeated; a pattern of other than literals, `RANDOM(#..)` and `SEQUENTIAL(#..)` fails |
| repeatable stage, `standardInterval` | how many events, at what spacing |
| program's category combo | an attribute option combo per event, uniform |

A default knows a field's type, not what it holds. If a query filters on a field, set its
[distribution](docs/distributions.md) in the spec, as its values shape the plan.

### What the spec adds

The [MNCH spec][mnch] of synthetic-databases fills DHIS2's ANC and EIR packages and sets most
keys. These are one or two lines of each key, the rest left out at `# ...`:

[mnch]: https://github.com/dhis2/synthetic-databases/tree/main/databases/mnch

```toml
[build]
metadata = ["metadata/anc.json", "metadata/eir.json", "metadata/mother-child.json"]
seed = 42
start = 2020-01-01
end = 2025-01-01
now = 2025-03-01 # "today" for scheduled and overdue events

[orgunits]
root = { name = "Perf Land", code = "QP" }

[[orgunits.level]]
name = "Region"
count = 20

[[orgunits.level]]
name = "District"
# Regions differ in their number of districts, and so in their people.
per_parent = { lognormal = { mean = 25, sigma = 0.64 }, min = 1, round = 0 }
# ...

[trait.district]
# Most districts are mid-sized, a few are large cities.
people = { lognormal = { mean = 300_000, sigma = 0.7 }, max = 1_500_000, round = 0 }
# ...

[trait.facility]
# Facilities of one type differ in how many people they serve.
size = { lognormal = { median = 1, sigma = 0.6 } }

[users]
officers = ["District", "Region"]
search_level = "District"

[program.RMNCAH_ANC]
tracked_entities = { total = "district.people * 0.125", among = "District", by = "facility.size * facility.weight" }
# ...

[program.RMNCAH_ANC.enrollments]
count = { poisson = 0.26, plus = 1 } # one pregnancy, and maybe more, one after another
gap = { uniform = [182, 546] } # from the last event to the next first contact
status = { COMPLETED = 0.6, ACTIVE = 0.4 } # of a woman's last pregnancy that ended
followup = 0.03

[program.RMNCAH_ANC.trait.enrollment]
# ...
hb = { normal = { mean = 11.2, sd = 1.5 }, min = 6, max = 16 } # g/dL
# ...

[program.RMNCAH_ANC.visits]
stages = ["Quick Check", "Physical Examinations", "Laboratory Tests and Imaging",
          "Symptoms and Follow-up", "Counselling and Treatment"]
count = { poisson = 4.0, plus = 1, max = "1 + floor(enrollment.days_to_outcome / 35)" }
spacing = { uniform = [28, 35] }
status = { COMPLETED = 0.97, SCHEDULE = 0.02, SKIPPED = 0.01 }

[program.RMNCAH_ANC.stage."Close ANC Record"]
share = 0.56
# Up to two weeks after the outcome.
offset = { sql = "enrollment.days_to_outcome + 1 + 14 * u('delay')" }

[program.RMNCAH_ANC.values]
# ...
Y0MfiRhxr1a = { bernoulli = "case when event.number = 0 then 0.7 else 0.3 end" } # haemoglobin ordered
# ...
yeZTdZuEZT6 = { normal = { mean = "enrollment.hb", sd = 0.6 }, min = 4, max = 18, if = "value.Y0MfiRhxr1a = 'true'" } # result, g/dL
# ...

[program.RMNCAH_ANC.attributes]
# ...
GIVEN_NAME = { words = { n = 20_000, s = 1.4, q = 25, vocabulary = "given" } }
# ...
"Mobile phone number" = { phone = true, fill = 0.7 }
# ...

[records]
updated = { share = 0.1, after = { exponential = 30 } }

[load]
order = "created" # rows in the order a live instance inserts them
```

* `[build]`: the `metadata` files in import order, the `seed`, and the data window from `start` to
  `end`, with `now` as "today".
* `[orgunits]`: the `root` and the levels below it. A level's org units are a `count` spread
  evenly over the parents, a number `per_parent`, which may be a distribution, or facility
  `types`, each with its density, weight and clerks.
* `[trait.<scope>]`: see [Traits](#traits).
* `[users]`: the `clerks` per org unit, the levels of view-only `officers`, the clerks'
  `search_level`, and the users' `prefix` and `password`.
* `[program.<code>]`: a program by its UID, code or name, the code and name ignoring case. Its
  tracked entities register at `orgunit_level`, the lowest by default. `tracked_entities` sets how
  many: `per_orgunit`, or a `total` shared `among` the org units of a level `by` a weight. Its
  tables:
  * `enrollments`: how many a tracked entity has (`count`), the `gap` between them, their
    `status`, and the share marked for `followup`, none by default.
  * `visits`: how many an enrollment has (`count`), the `spacing` between them, their `status`,
    and their `stages`.
  * `stage."<name>"`: the events of a stage outside the visits: how many, when, and their
    `status`. Of any stage, the share of its events `assigned` to their clerk, if its metadata
    enables user assignment; none by default. A stage of the visits takes only `status` and
    `assigned`.
  * `values`: how to draw an event's data values, by data element code, UID or name. A value is
    written only `if` its condition holds, and at the share its `fill` gives, such as
    `{ constant = true, if = "event.number = 1", fill = 0.95 }`. A data element left out stays
    empty, unless it is compulsory: then it takes its default.
  * `attributes`: the same for a tracked entity's attributes, drawn once, at its registration.
  * `from`: see [Linking programs](#linking-programs).
  * `unvalidated`: see [Program rules](#program-rules).
* `[records]`: the share of records `updated` after their creation, and the days `after` it.
* `[load]`: the `order` of the rows: `created`, as a live instance inserts them, the default, or
  `parallel`, which loads faster and interleaves them on disk.

`compile` fails on a name the metadata does not hold, an option code its option set does not hold,
a string its field does not take, and an expression [DuckDB][duckdb] cannot bind, that names a
scope it does not see, or that is not one expression on its own.

[duckdb]: https://duckdb.org/

### Distributions and expressions

A value is a constant or a distribution, such as `normal`, `poisson`, or `sql` for a DuckDB
expression. The modifiers `min`, `max`, `plus` and `round` bound, shift or round it.
[Distributions](docs/distributions.md) plots each distribution and modifier, and helps pick one.
The text generators, such as `words`, `phone` and `email`, take no modifier.

A string is text, written as it is. An expression needs `sql`: `HB = { sql = "enrollment.hb + 1" }`
computes a haemoglobin, while `HB = "enrollment.hb + 1"` is text, which compile rejects for a
number. A trait is a number, so never a string.

A parameter or `sql` can be an expression over the record and the levels above it, such as an
event's enrollment, tracked entity, facility and district:

* `district.people`, `facility.type`: an org unit's traits, and its `code`, `name`, `uid`, `type`
  and `weight`, by its level's name in lower case with `_` for spaces and punctuation, such as
  `example_land` for the root
* `orgunit.code`, `orgunit.uid`: the record's org unit
* `tracked_entity.<trait>`, `enrollment.<trait>`, `enrollment.date`, `enrollment.number`
* `event.date`, `event.number`, `event.status`
* `record.date`: the event's date for a data element, the enrollment's for an attribute
* `value.<UID>`: another value of the same event, such as a weight category from a BMI
* `u('x')`, `n('x')`: a uniform and a standard normal draw of the record, the same for one name
  within a field; a name holds lowercase letters, digits and `_`

An expression can use only what is drawn before it:

```
org units ─▶ tracked_entity ─▶ enrollment ─▶ event
```

An enrollment's trait can use `district.people` and `tracked_entity.age`, but not `event.date`.
Where an expression names more, `explain` and `compile` fail and say what it can use:

```
trait.enrollment.x: names event.number, but sees only district, enrollment, example_land, facility, orgunit, region, tracked_entity here
```

Dates are days since 1970-01-01 and durations are days, so a due date 40 weeks after the last
menstrual period is `enrollment.lmp + 280`.

A spec is code: an expression is any DuckDB SQL. dbgen confines it to the files of the build's
stages, so it reads no other file and no URL, and changes no DuckDB setting. It can still run long
or take all the memory DuckDB has, so build only specs you trust.

### Traits

The Postgres planner multiplies the selectivities of filters as if columns were independent (see
[Row Estimation Examples][pg-rows]). Traits make them depend on each other, as in production, so
the planner can be wrong as it is there. In the example, the facility trait `size` sets a
facility's share of its district's tracked entities, and the enrollment trait `hb` sets the
haemoglobin of every lab test of a pregnancy.

[pg-rows]: https://www.postgresql.org/docs/current/row-estimation-examples.html

A level's traits are in `[trait.<level>]`, by its name in lower case. Those of tracked entities and
enrollments are in `[trait.tracked_entity]` and `[trait.enrollment]`, or a program's own in
`[program.<code>.trait.<scope>]`. An expression names a trait by its scope, such as
`facility.size`.

A trait is a value or a distribution. A level's trait may instead rank its org units:
`urban = { largest = "people", until_share = 0.45 }` is 1 for the org units with the most `people`
until they hold 45% of the level's, and 0 for the others.

### Linking programs

A program can derive its tracked entities from another program's enrollments, such as children
from pregnancies in the example:

```toml
[program.CHILD.from]
enrollments = { of = "PREGNANCY", as = "mother" } # a pregnancy, called mother
where = "mother.live_birth = 1" # a trait of the pregnancy
share = 0.9 # of those, the children who come to a facility
relationship = "Mother and child" # a relationship type of the metadata
at = "mother.date + mother.days_to_birth" # the child's enrollment date

[program.CHILD.from.attributes]
FAMILY_NAME = "mother.FAMILY_NAME" # the mother's
```

`as` names the source enrollment in the program's expressions: its traits and its tracked
entity's, and its `number`, `date`, `end` and `status`. A linked tracked entity registers at its
source's org unit. The program may draw tracked entities of its own besides, for which `mother` is
null. `from.attributes` copies the source's attributes; a value computed from `mother` goes in
`attributes` or `values`, such as `{ sql = "coalesce(mother.end + 7, ...)" }`.

### Program rules

`load` writes past DHIS2's importer, so the program rules do not run. The spec must draw values
the rules accept, and fill what a rule's `ASSIGN` would. `dbgen validate` checks this, and `verify`
does the same for a sample of every build.

A rule that computes from today's date, such as an age at booking, cannot hold for a record of the
past. Fill such a field as a live instance holds it, and list it in its program's `unvalidated`,
which `validate` and `verify` leave out.

### Check a spec

* `dbgen explain <spec>` prints the effective spec, table by table: the keys the spec sets, then
  what the metadata and dbgen's defaults give the others. A key the spec sets over the metadata
  says so, such as `spacing 7 days [over standardInterval 28]`. It shows how each field is drawn,
  how many optional fields each stage leaves empty, and the records each program expects. It fails
  where `compile` would.
* `dbgen sample <spec> --orgunit <code>` writes the records of an area as Parquet files to
  `output/stage/<build>/generate/`, without Docker.
* `dbgen validate <spec> --orgunit <code>` submits every record of a small area to DHIS2's
  importer with `importMode=VALIDATE`, which runs the program rules and writes nothing. It reports
  the errors and warnings by code, rule and field, and fails on any error.

### Inspect the Parquet files

The stages up to `bootstrap` write Parquet files to `output/stage/<build>/<stage>/`.
`dbgen describe` prints what one holds: its rows, metadata, and each column's type and
description. The [DuckDB CLI](https://duckdb.org/docs/installation/) queries the files directly;
cap its memory, as it takes 80% of RAM by default:

```sh
uv run dbgen describe output/stage/<build>/generate/event.parquet
duckdb -c "set memory_limit = '2GB';
  summarize from 'output/stage/<build>/generate/event.parquet'"
```

## How a build works

A build is a pipeline of stages, each reading the outputs of the stages before it:

```
compile ─▶ orgunits ─▶ users ─┬─▶ generate ─────┐
                              └─▶ bootstrap ────┴─▶ load ─▶ verify ─▶ dump
```

| Stage | What it does | Runs in |
|---|---|---|
| `compile` | read the metadata and the spec, check every name and option, write the effective spec and the SQL of `generate` | Python |
| `orgunits` | draw the org unit hierarchy from the spec's levels | DuckDB |
| `users` | make the clerks, officers and admin | DuckDB |
| `generate` | draw the tracker data and write it as DHIS2's tables, then check it | DuckDB |
| `bootstrap` | start DHIS2 on an empty database, import the metadata and a few org units and users, copy the others in SQL | DHIS2, Postgres |
| `load` | bulk load the tracker tables, then build the indexes and analyze | Postgres |
| `verify` | check the loaded database, and send a sample of records through DHIS2's importer | DHIS2, Postgres |
| `dump` | dump the database and write its manifest | Postgres |

`uv run dbgen stage <stage> <spec>` runs one stage on its own, once the stages it reads have run;
`--help` lists what each reads and writes.

### What a build writes

Nothing a build writes is committed:

```
output/
├── stage/<build>/
│   ├── <stage>/                    each stage's outputs, which a stage run on its own reads
│   ├── timings/                    wall and CPU time, peak memory and rows of generate on
│   └── provenance.json             what each stage ran with and read
└── <build>/                        the dump, dhis2-db-<build>.sql.gz, and its manifest.json
```

`dump` copies the timings and the provenance into `manifest.json`, so they stay with the dump once
`output/stage/` is deleted.

### Design choices

* **Metadata through DHIS2's API, what is too large for it in SQL**: the tracker data goes in
  through Postgres `COPY`, whose validation the [checks](#checks) stand in for. Of the org units
  and the users, the API imports a few of each level or kind, and `bootstrap` copies the others in
  SQL from them, then assigns the programs to their org units.
* **Metadata by UID until `load`**: `generate` writes DHIS2's tables with the metadata's UIDs,
  which `load` swaps for `bootstrap`'s internal IDs as it copies, and builds `eventdatavalues` from
  lists of values. So `generate` needs no DHIS2, and a spec change reruns `generate` and `load`,
  not `bootstrap`.
* **Draws as hashes** of the seed, the record's key and the field's path in the spec, so a build
  does not depend on row order or threads, and a new field shifts no other's draws.
* **Rows in the order of creation**, as a live instance inserts them, so the planner sees the
  dates spread over the table as they are in production.
* **The program rules are the spec's to satisfy**: `dbgen` does not read them, and `validate`
  checks them.

### Checks

* **Pre-load**, in `generate`: from the metadata, the value types, option codes, compulsory data
  elements, mandatory and unique attributes, repeatable stages, dates in order and the org units
  of programs (`src/dbgen/sql/checks/preload.sql`). Any violation fails the build.
* **Post-load**, in `verify`: SQL checks of what DHIS2 writes, such as each data value's fields,
  the trigram indexes of searchable attributes, a sample of every program's records exported and
  submitted again to the tracker importer with `importMode=VALIDATE`, and the tracker API's queries
  as an admin and as a clerk.

### Determinism

With the same metadata, spec and seed, two builds give the same row digests of the org units, the
users and every tracker table, which `generate` writes to `checksums.json` and `manifest.json`
copies. The dump's bytes differ, as it holds import and login times. DuckDB is pinned, as a
version that changes `hash()`, as 1.3 did, changes every draw.

### Provenance

Every stage records in `output/stage/<build>/provenance.json` the build options, a digest of each
part of the spec, dbgen's version and git commit, whether its tree was clean, the same of the
repository that holds the spec, and the records of the stages it read. Installed from git, dbgen's
commit is the one installed; installed otherwise, it is unknown. A stage refuses inputs that do
not fit together, such as a `generate` of another spec than `bootstrap`'s. `manifest.json` keeps
these records.

## Development

```sh
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

The tests need no Docker. `uv run pre-commit install` runs ruff and ty on every commit. CI runs the
same hooks, the tests and a build of the example's district on `main` and on pull requests.

### Release

A release is a tag `vX.Y.Z` on `main`, pushed to GitHub. Nothing is published: a repository that
uses dbgen, such as [synthetic-databases][synthetic-databases], installs it from git by its tag.
The version comes from the tag, so `dbgen --version` prints `dbgen 0.2.0 (commit 1a2b3c4)` on the
tag and `dbgen 0.2.1.dev3 (commit 5d6e7f8)` three commits after it, with `uncommitted changes` if
the checkout has any.

Installed, dbgen ships its images and their compose setup, and writes `output/` in the working
directory, or in `--root`. There, `dbgen lock` checks the packages pinned in
`databases/<name>/packages.lock.toml` against their metadata files, or takes a new version of one.

## Licence

BSD 3-Clause (`LICENSE`), as DHIS2 core.
