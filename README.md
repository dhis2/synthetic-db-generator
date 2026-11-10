# DHIS2 Synthetic Database Generator

Synthetic DHIS2 tracker databases for performance testing. `dbgen` reads a DHIS2 metadata export
and a spec, and builds a Postgres dump that the [DHIS2 performance test setup][perf-setup]
restores like any other database. The same metadata, spec and seed give the same data (see
[Determinism](#determinism)).

[perf-setup]: https://github.com/dhis2/dhis2-core/tree/master/dhis-2/dhis-test-performance

`dbgen` builds a dump for DHIS2 2.42.0, the image pinned in `src/dbgen/lock.toml`.
[synthetic-databases][synthetic-databases] builds the databases to publish with it, such as Perf
Land MNCH, upgrades them to each later DHIS2 version and publishes a dump for each. Look there for
those databases and for a realistic spec.

[synthetic-databases]: https://github.com/dhis2/synthetic-databases

## Quick start

[The example](#the-example) is a small spec of pregnancies and the children born of them. These
need no Docker:

```sh
uv run dbgen areas examples/pregnancy/pregnancy.toml --orgunit EX02    # the areas to build
uv run dbgen explain examples/pregnancy/pregnancy.toml                 # how each field is drawn
uv run dbgen sample examples/pregnancy/pregnancy.toml --orgunit EX0201 # its records as Parquet
```

This builds district EX0201 into a dump of DHIS2's database, in about 2 minutes (see
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
* **Org units**: a hierarchy the spec draws, a root and levels below it, such as districts and
  facilities, each with a code that `dbgen areas` lists.
* **Area**: the org units a build covers, by their codes, each with every org unit below it.
* **Records**: a tracked entity registers at an org unit and has enrollments in a program, each
  with events of the program's stages. A visit is one event of each of several stages, on one day.
* **Trait**: a hidden value of an org unit, tracked entity or enrollment that fields depend on
  (see [Traits](#traits)).

## Limitations

* Tracker programs only: an event program fails.
* No record is deleted.
* An event is entered on its day, never later.

## Write a spec

Only `[build].metadata` is required. With nothing else, `dbgen` fills every compulsory field with
values its metadata allows, by the defaults below, and leaves the others empty. Such values pass
the importer's checks but not always the program rules, which the spec satisfies (see
[Program rules](#program-rules)).

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

### The spec, key by key

The [MNCH spec][mnch] of synthetic-databases fills DHIS2's ANC and EIR packages and sets every key.
These are lines of it, one or two of each key, the rest left out at `# ...`:

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

* `[orgunits]`: the root and the levels below it. A level's org units are a `count` spread evenly
  over the parents, a number `per_parent`, which may be a distribution, or facility `types`, each
  with its density, weight and clerks.
* `[program.<code>]`: a program by its UID, else its code, else its name, ignoring case, the
  `orgunit_level` it registers at, the lowest by default, and how many tracked entities register
  at each org unit of it (`per_orgunit`, or a `total` shared `among` the org units of a level `by`
  a weight).
* `enrollments`: how many a tracked entity has (`count`), the `gap` between them, their `status`,
  and the share marked for `followup`, none by default.
* `visits`: how many an enrollment has, the `spacing` between them, their statuses, and their
  `stages`.
* `stage."<name>"`: the events of a stage outside the visits, when, and their statuses. Of any
  stage, the share of its events `assigned` to their clerk, if its metadata enables user
  assignment; none by default. A stage of the visits takes only `status` and `assigned`.
* `values`: how to draw an event's data values, by data element, its code, UID or name, written
  only `if` its condition holds and at the share its `fill` gives, such as
  `{ constant = true, if = "event.number = 1", fill = 0.95 }`. A data element left out stays
  empty, unless it is compulsory: then it takes the metadata's default.
* `attributes`: the same for a tracked entity's attributes, drawn once, at its registration.
* `[users]`: the `clerks` per org unit, the levels of view-only `officers`, the `search_level`,
  and the users' `prefix` and `password`.
* `[records]`: the share of records `updated` after their creation, and the days `after` it.
* `[load]`: the `order` of the rows, `created` as a live instance inserts them, the default, or
  `parallel`, which loads faster and interleaves them on disk.

`compile` fails on a name the metadata does not hold, an option code its option set does not hold,
a string its field does not take, and an expression [DuckDB][duckdb] cannot bind, that names a
scope it does not see, or that is not one expression on its own.

[duckdb]: https://duckdb.org/

### Distributions and expressions

A value is a constant or a distribution, such as `normal`, `poisson` or `sql` for a DuckDB
expression. The modifiers `min`, `max`, `plus` and `round` bound, shift or round it.
[Distributions](docs/distributions.md) pictures each distribution and modifier, and helps pick
one. `words`, `phone`, `email`, `pattern` and `unique` make text, and take no modifier.

A string is text, written as it is, and `sql` takes a DuckDB expression:
`HB = { sql = "enrollment.risk + 1" }` computes a haemoglobin, while `HB = "enrollment.risk + 1"`
writes that text, which compile rejects, as a haemoglobin is a number. A trait is a number, so
never a string.

A parameter or `sql` may be an expression over the record and the levels above it, such as an
event's enrollment, tracked entity, facility and district:

* `facility.size`, `facility.type`: an org unit's traits, name and code, by its level's name in
  lower case with `_` for spaces and punctuation, such as `district.people` for District and
  `perf_land` for the root
* `orgunit.code`, `orgunit.uid`: the record's org unit
* `tracked_entity.<trait>`, `enrollment.<trait>`, `enrollment.date`, `enrollment.number`
* `event.date`, `event.number`, `event.status`
* `record.date`: the event's date for a data element, the enrollment's for an attribute
* `value.<UID>`: another value of the same event, such as a weight category from a BMI
* `u('x')`, `n('x')`: a uniform and a standard normal draw of the record, the same for one name
  within a field; a name holds lowercase letters, digits and `_`

An expression can use what is drawn before what it draws:

```
org units ─▶ tracked_entity ─▶ enrollment ─▶ event
```

An enrollment's trait can use `district.people` and `tracked_entity.weight`, but not `event.date`.
Where an expression names more, `explain` and `compile` fail and say what it can use:

```
trait.enrollment.x: names event.number, but sees only district, enrollment, facility, orgunit, perf_land, region, tracked_entity, ward here
```

Dates are days since 1970-01-01 and durations days, so a due date 40 weeks after the last
menstrual period is `enrollment.lmp + 280`.

A spec is code: an expression is any DuckDB SQL, which compile binds and orgunits and generate
run. dbgen confines it to the files of the build's stages, so it reads no other file and no URL,
and changes no DuckDB setting. It can still run for long or take all the memory DuckDB has, so
build only specs you trust.

### Traits

The Postgres planner multiplies the selectivities of filters as if columns were independent (see
[Row Estimation Examples][pg-rows]). Traits make them dependent, as in production, so the planner
can be wrong as it is there. In [the example](#the-example), the facility trait `size` sets a
facility's share of its district's tracked entities, and the enrollment trait `hb` sets the
haemoglobin of every lab test of a pregnancy.

[pg-rows]: https://www.postgresql.org/docs/current/row-estimation-examples.html

A level's traits are in `[trait.<level>]`, by its name in lower case. Those of tracked entities
and enrollments are in `[trait.tracked_entity]` and `[trait.enrollment]`, or a program's own in
`[program.<code>.trait.<scope>]`. An expression names one by its scope, such as `facility.size`.

A trait is a value or a distribution, and a level's may instead rank its org units:
`urban = { largest = "people", until_share = 0.45 }` is 1 for the org units of the most `people`
until they hold 45% of its sum, 0 for the others.

### Linking programs

A program can derive its tracked entities from another's enrollments, such as children from
pregnancies in [the example](#the-example):

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
entity's, its `number`, `date`, `end` and `status`. A linked tracked entity registers at its
source's org unit. The program may draw tracked entities of its own besides, for which `mother` is
null. `from.attributes` copies the source's attributes; a value computed from `mother` goes in
`attributes` or `values`, such as `{ sql = "coalesce(mother.end + 7, ...)" }`.

### Program rules

`load` writes past DHIS2's importer, so the program rules do not run: the spec draws values the
rules accept, and fills what a rule's `ASSIGN` would. `dbgen validate` checks it (see
[Check a spec](#check-a-spec)), and `verify` does the same for a sample of every build.

A rule that computes from today's date, such as an age at booking, cannot hold for a record of the
past. The spec fills such a field as a live instance holds it, and lists it in its program's
`unvalidated`, which `validate` and `verify` leave out.

### Check a spec

* `dbgen explain <spec>` prints the effective spec: every field that gets a value with how its
  values are drawn and where that came from, the metadata or the spec, how many optional fields
  are left empty, and the records it expects. It fails where `compile` would.
* `dbgen sample <spec> --orgunit <code>` generates the records of an area as Parquet files (see
  [Inspect the Parquet files](#inspect-the-parquet-files)).
* `dbgen validate <spec> --orgunit <code>` submits every record of a small area to DHIS2's
  importer with `importMode=VALIDATE`, which runs the program rules and writes nothing, and
  reports the errors and warnings by code, rule and field. Any error fails.

## Build a database

### Requirements

* Linux, [uv](https://docs.astral.sh/uv/) and Docker
* 17 GiB of free memory: 4 GiB for Postgres, 5 GiB for DHIS2, and 8 GiB for DuckDB and the rest
  of the machine. DHIS2 and Postgres refuse to start without it. A smaller machine lowers the
  caps (see [Caps](#caps)).
* disk: about 1.5 GiB per million events in the build database, the Docker volume
  `dbgen-<checkout>-<hash>_pgdata` of each checkout, with up to 4 GiB of WAL besides, and 0.25 GiB
  in the dump: MNCH's district QP1613, 2.2 million events, takes 6.4 GiB and 0.5 GiB. DHIS2 and
  Postgres refuse to start without 10 GiB free, and `load` without 8 GiB and 1.5 GiB per million
  events.

### Build

```sh
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
```

This builds district EX0201 of [the example](#the-example), 518 tracked entities and 4,055 events,
in about 2 minutes into `output/pregnancy-EX0201/`, a dump of half a MiB. The first build also
pulls the Docker images pinned in `src/dbgen/lock.toml`. Every build empties the build database
of its checkout first, so run one at a time in a checkout. Builds of several checkouts run side by
side, each with its database, as far as the memory allows (see [Requirements](#requirements)).
dbgen prints the host ports of Postgres and DHIS2 at each start. The dumps in `output/` stay.

### Choose the area

`dbgen areas <spec> --orgunit <code>` lists an org unit, the root without `--orgunit`, and its
children, with the tracked entities and events a build of each would hold, and the traits of each:

```
$ uv run dbgen areas examples/pregnancy/pregnancy.toml --orgunit EX02
code    level     name       children  people  tracked entities  events
EX02    Region    Pubek             3                     3,610  32,072
EX0201  District  Soyonimax         4  15,373               640   5,682
EX0202  District  Molarofuk         4  44,180             1,838  16,329
EX0203  District  Takot             4  27,221             1,132  10,061
```

The tracked entities and events are estimates from the spec's means: a build holds fewer.

A build covers the org units under the codes `--orgunit` gives, or all of them without it. Its
name, and the directory of its dump, is the spec's name and the code, or `--name` for several:

```sh
# output/pregnancy-EX0201/
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
# output/pregnancy-both/
uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201 EX0202 --name both
```

Its database holds every org unit down to the area's level, and below it those of the area, with
the area's users and records.

### Caps

Postgres and DHIS2 run capped, and DHIS2 and Postgres start only when the memory of their caps
and a headroom for DuckDB and the rest of the machine are free. Each cap is an environment
variable:

| variable | default | |
|---|---|---|
| `DBGEN_DB_MEMORY` | `4g` | Postgres's memory |
| `DBGEN_DB_CPUS` | `4` | Postgres's CPUs, and the streams of a parallel `load` |
| `DBGEN_WEB_MEMORY` | `5g` | DHIS2's container's memory |
| `DBGEN_WEB_CPUS` | `4` | DHIS2's CPUs |
| `DBGEN_WEB_HEAP` | `3g` | DHIS2's Java heap, less than its container's memory |
| `DBGEN_HEADROOM` | `8g` | the memory left free beside them |

A build of up to about 100,000 events, such as MNCH's ward QP0602-02, fits a standard GitHub
runner, 4 CPUs and 16 GB, with:

```sh
DBGEN_DB_MEMORY=3g DBGEN_WEB_MEMORY=4g DBGEN_WEB_HEAP=2500m DBGEN_HEADROOM=3g \
  uv run dbgen build examples/pregnancy/pregnancy.toml --orgunit EX0201
```

### Run it installed

dbgen runs from its checkout with `uv run`, or installed, such as a dependency of
[synthetic-databases][synthetic-databases], which holds the databases. It ships its images and
their compose setup, and writes `output/` in the working directory, as `--root` says. `dbgen lock`
checks the databases of the working directory's `databases/`.

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

### Use a dump

Core's [performance test setup][perf-setup] restores its databases from S3 and takes no local dump
yet. A dump is plain SQL, which `psql` restores into an empty database of Postgres 16 with
PostGIS, such as the `postgres` image pinned in `src/dbgen/lock.toml`:

```sh
createdb dhis
gunzip --stdout output/pregnancy-EX0201/dhis2-db-pregnancy-EX0201.sql.gz | psql --dbname dhis
```

Run DHIS2 on it with the image its `manifest.json` names in `images.dhis2`, the one it was built
on: another may migrate the schema at start or fail on it.

The users, for performance testing only, have the password `Perf-Test-2026!` and the spec's name
as prefix, unless `[users]` sets them:

* `admin` / `district`: DHIS2's default superuser
* `<prefix>.<org unit code>.<n>`: a clerk of an org unit a program registers at
* `<prefix>.<level>.<org unit code>`: a view-only officer of a level of `users.officers`
* `<prefix>.admin`: an admin with the ALL authority

Usernames are in lower case, such as `pregnancy.ex0201-01.1` and `pregnancy.district.ex0201`.

## The example

`examples/pregnancy/` builds data of two made-up tracker programs for Example Land, a made-up
country of 2 regions, 6 districts and 24 facilities:

* `pregnancy.toml`: the spec, with a comment per choice. A woman has one pregnancy or more, each a
  profile, monthly visits of a check and a lab test, and a closing event after the birth.
* `metadata.json`: the Pregnancy and Child programs, with a few fields of each value type.

A live birth becomes a child in Child, linked to its mother, with a birth event and vaccinations.
The spec sets every compulsory field, and leaves optional ones such as Notes empty, as `dbgen
explain` shows.

For a realistic spec of DHIS2's own packages, see [Perf Land MNCH][mnch] in synthetic-databases.

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
`--help` lists them.

### Design choices

* **Metadata through DHIS2's API, what is too large for it in SQL**: the tracker data through
  Postgres `COPY`, whose validation the [checks](#checks) stand in for. Of the org units and the
  users, the API imports a few of each level or kind, and `bootstrap` copies the others in SQL
  from them, then assigns the programs to their org units.
* **Metadata by UID until `load`**: `generate` writes DHIS2's tables with the metadata's UIDs,
  which `load` swaps for `bootstrap`'s internal IDs as it copies, and builds `eventdatavalues` from
  lists of values. So `generate` needs no DHIS2, and a spec change reruns `generate` and `load`,
  not `bootstrap`.
* **Draws as hashes** of the seed, the record's key and the field's path in the spec, so a build
  does not depend on row order or threads, and a new field shifts no other's draws.
* **Rows in the order of creation**, as a live instance inserts them, so the planner sees the
  dates spread over the table as they are in production.
* **The program rules are the spec's to satisfy**: `dbgen` does not read them, and
  `validate` checks them.

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
copies. The dump's bytes differ: it holds import and login times. DuckDB is pinned, as a version
that changes `hash()`, as 1.3 did, changes every draw.

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

`uv run pre-commit install` runs ruff and ty on every commit. CI runs the same hooks and the tests
on `main` and on pull requests. The tests need no Docker.

### Release

A release is a tag `vX.Y.Z` on `main`, pushed to GitHub. Nothing is published: a repository that
uses dbgen, such as [synthetic-databases][synthetic-databases], installs it from git by its tag.
The version comes from the tag, so `dbgen --version` prints `dbgen 0.2.0 (commit 1a2b3c4)` on the
tag and `dbgen 0.2.1.dev3 (commit 5d6e7f8)` three commits after it, with `uncommitted changes` if
the checkout has any.

### Inspect the Parquet files

`dbgen describe` prints what a Parquet file of `output/stage/` holds: its rows, metadata, and its
columns with their type and description:

```sh
uv run dbgen describe output/stage/<build>/generate/event.parquet
```

The [DuckDB CLI](https://duckdb.org/docs/installation/) queries them directly. Cap its memory, as
it takes 80% of RAM by default:

```sh
duckdb -c "set memory_limit = '2GB';
  summarize from 'output/stage/<build>/generate/event.parquet'"
```

### Update a package

`dbgen lock` checks the packages of `databases/<name>/packages.lock.toml` against their metadata
files, or takes a new version of one. It runs in a repository of databases, such as
[synthetic-databases][synthetic-databases].

## Licence

BSD 3-Clause (`LICENSE`), as DHIS2 core.
