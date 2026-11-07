# Distributions

A value in the spec is a constant or a distribution, changed by modifiers (see the README's
[Distributions and expressions](../README.md#distributions-and-expressions)). This guide helps
you pick one. Each distribution comes with a spec snippet and a plot of 100,000 values drawn from
it. `docs/plots.py` draws them with dbgen's own code, so a plot shows what a build writes.

## Pick a distribution

| What you know | Use |
|---|---|
| only the bounds | [`uniform`](#uniform) |
| a typical value, as likely above it as below | [`normal`](#normal) |
| a positive size, most small and a few large | [`lognormal`](#lognormal) |
| the average time until something happens | [`exponential`](#exponential) |
| the share of records for which something is true | [`bernoulli`](#bernoulli) |
| the average of a count | [`poisson`](#poisson) |
| the share of each of a set of values | [`weights`](#weights) |
| the same value for every record | [`constant`](#constant) |
| a value computed from others, or a shape no key draws | [`sql`](#sql) |

A parameter can also be an expression over other values, such as a rate that grows with a
district's people (see [Make a value depend on another](#make-a-value-depend-on-another)).

## The distributions

### uniform

```toml
spacing = { uniform = [28, 35] }
```

![uniform](img/uniform.svg)

Any value from low up to high, each as likely. High itself is excluded, so a date range never
gives its last day. Rounded to whole numbers, low and high come up half as often as the others:
`[27.5, 35.5]` gives each of 28 to 35 alike.

Few real quantities are flat: a mother's age as `uniform = [15, 45]` makes as many 15-year-olds as
25-year-olds. Where a source gives the share of each range, such as of each age group, draw by those
shares (see [Shares of ranges](#shares-of-ranges)).

### normal

```toml
height = { normal = { mean = 158, sd = 6 } }
```

![normal](img/normal.svg)

Values around the mean you give, as many above as below: `mean = 158, sd = 6` gives 95% between 146
and 170. It fits a measurement, such as a height or a haemoglobin. It has no bounds, so a large
build draws a few extreme values, ones no person has or a program rule might reject. Give it `min`
and `max`, which truncate it (see [Bounds](#bounds-shifts-and-rounding)).

### lognormal

```toml
people = { lognormal = { mean = 300_000, sigma = 0.7 } }
```

![lognormal](img/lognormal.svg)

Positive and skewed to the right: sizes, such as the people of a district or the patients of a
facility. It makes the few busy districts and facilities, which hold most of the records.

Give it the median or the mean, whichever a source gives. `sigma` sets the spread: 0.7 puts about
two thirds of the values between half and twice the median. The larger `sigma`, the longer the
tail, and the further it pulls the mean above the median:

$$\text{median} = \frac{\text{mean}}{e^{\sigma^2 / 2}}$$

### exponential

```toml
after = { exponential = 30 }
```

![exponential](img/exponential.svg)

A time until something happens, from 0 up, around the mean you give, such as the days until a record
is updated. Short times are common and long ones rare: for a mean of 30 days, half the records are
updated within 21 days, and 14% wait over 60. If you know the median rather than the mean, the mean
is the median divided by 0.69: half within 14 days is `exponential = 20`.

### bernoulli

```toml
facility_delivery = { bernoulli = 0.45 }
```

![bernoulli](img/bernoulli.svg)

1 or 0, with the share of 1s you give: `bernoulli = 0.45` gives 1 to 45% of the records. A yes or no
field writes them as `true` and `false`. For any other pair of values, such as two options, use
[`weights`](#weights).

### poisson

```toml
visits = { poisson = 4.0, plus = 1 }
```

![poisson](img/poisson.svg)

A whole number, 0 or more, around the mean you give: `poisson = 4` gives 4 on average, mostly
2 to 6. `plus = 1` makes it at least one. The spread follows from the mean and is small: here 1.9%
of the women have one visit and 2.1% ten or more. Real counts per patient spread more, see [Counts
with a long tail](#counts-with-a-long-tail).

### weights

```toml
education = { weights = { NONE = 35, PRIMARY = 20, SECONDARY = 35, HIGHER = 10 } }
```

![weights](img/weights.svg)

One of the keys, each as often as its weight: `NONE = 35` out of a total of 100 gives NONE to 35%
of the records. For an option set, the keys are its option codes. The weights are relative:
`{ A = 3, B = 1 }` draws as `{ A = 0.75, B = 0.25 }`.

### constant

```toml
iron = 60
iron = { constant = 60, fill = 0.8 }
```

The same value for every record, written as it is: `iron = 60`. To give it an `if` or a `fill`,
as a data value or an attribute may have, write it as a table with the value under `constant`:
`{ constant = 60, fill = 0.8 }` writes 60 mg of iron in 80% of the events and leaves the others
empty.

### sql

```toml
ga_first = { sql = "case when u('m') < 0.35 then 6 + 7 * u('w') when u('m') < 0.75 then 13 + 9 * u('w') else 22 + 14 * u('w') end" }
```

![sql](img/sql.svg)

A DuckDB expression: a value computed from others, such as a weight from a BMI and a height, or a
shape no key draws. `u('x')` gives a random number from 0 to 1, and `n('x')` one from a normal of
mean 0 and sd 1. The same name gives the same number within a field. Here the gestational age at the
first visit is 35% in weeks 6 to 13, 40% in weeks 13 to 22 and the rest in weeks 22 to 36. Where a
key fits, use it: it is easier to read and review.

## Bounds, shifts and rounding

`min` and `max` truncate a [`normal`](#normal) and clip any other distribution. Both bounds are
included: a value can be `min` or `max` itself.

A truncated normal draws only from the range, so it keeps its shape and nothing piles up at a
bound:

```toml
hb = { normal = { mean = 11.2, sd = 1.5 }, min = 9, max = 14 }
```

![truncated](img/truncated.svg)

A clipped value outside the range is set to the bound, so the draws beyond it pile up there, in
red. The same normal drawn by [`sql`](#sql) is clipped:

```toml
hb = { sql = "11.2 + 1.5 * n('hb')", min = 9, max = 14 }
```

![clipped](img/clipped.svg)

7.1% of the values are exactly 9, which Postgres's statistics hold as the column's most common
value. Clipping is right when the bound is a real ceiling, such as a schedule of six visits: no
child gets a seventh.

```toml
visits = { poisson = 5.4, min = 1, max = 6 }
```

![clipped-count](img/clipped-count.svg)

The 30% drawn above six are set to six, so 45% of the children complete the schedule.

* `plus = n` adds n to the value drawn, before `min` and `max`. To a normal it adds to the mean.
* `round = n` rounds to n decimals, after everything else. `round = 0` gives a whole number.

## Make a value depend on another

A parameter can be an expression over other values of the record and of the levels above it, such
as an event's enrollment, tracked entity, facility and district (see the README's
[Traits](../README.md#traits)):

```toml
facility_delivery = { bernoulli = "0.45 + 0.35 * district.urban" }
hb = { normal = { mean = "enrollment.hb", sd = 0.6 }, min = 4, max = 18 }
```

The Postgres planner assumes evenly spread, independent columns, so data drawn column by column
hides the misestimates and slow plans that real data can cause. The misestimates are largest where
values depend on each other across tables, such as an event's value on its district, and grow with
each join ([Leis et al.][leis], [DSB][dsb]).

## What shapes performance

* **The rows per entity**: tracked entities per facility, enrollments per tracked entity, events
  per enrollment. Their skew makes the few busy facilities and the many quiet ones, and a query
  of a busy one is the one that is slow. The shapes of values matter less, unless a query filters
  or groups on them.
* **Selectivity decides the plan**: the same query of a busy and a quiet org unit can take an
  index or a scan, so test both ([Leis et al.][leis], [DSB][dsb]).
* **A wrong estimate is not always a bad plan**: it depends on the indexes, and data in memory
  hides bad plans, so a build larger than memory shows more ([Leis et al.][leis]).
* **Builds of different areas don't compare directly**: a district of twice the people need not
  be twice the work of a query, as their busy facilities differ.

## Shapes without a key

### Counts with a long tail

To spread counts more, as counts per patient are, draw the mean itself for each record, so some
records get a high mean and some a low one:

```toml
visits = { poisson = "4.0 * exp(0.8 * n('rate') - 0.32)", plus = 1 }
```

![overdispersed](img/overdispersed.svg)

The mean is still 5, as $e^{0.8 n - 0.32}$ for a standard normal $n$ has a mean of 1
($0.32 = 0.8^2 / 2$), but the variance is 18 instead of 4: 12% of the women have one visit and
11% ten or more. The larger the 0.8, the longer the tail.

### Shares of ranges

A source often gives the share of each range of a value rather than its shape, such as the share
of mothers aged 15 to 19, 20 to 24 and so on. Draw such a value in two steps: `u('range')` picks
a range by its share, and `u('age')` a value within it.

```toml
age = { sql = "case when u('range') < 0.15 then 15 + 5 * u('age') when u('range') < 0.45 then 20 + 5 * u('age') when u('range') < 0.70 then 25 + 5 * u('age') else 30 + 15 * u('age') end" }
```

![ranges](img/ranges.svg)

15% of the mothers are 15 to 19, 30% 20 to 24, 25% 25 to 29 and 30% 30 to 45. Every `when`
compares the same draw `u('range')` with the shares added up: 0.15, 0.15 + 0.30 = 0.45, and
0.45 + 0.25 = 0.70. The gestational age in [sql](#sql) is drawn the same way.

## Names and other words

`words` writes a made-up word: it draws a rank from 1 to `n` of a vocabulary, and every record of
a rank gets the same word. Fields of the same `vocabulary` share their words, so a mother's and
her child's family names can match.

```toml
GIVEN_NAME = { words = { n = 20_000, s = 1.4, q = 25, vocabulary = "given" } }
FAMILY_NAME = { words = { n = 100_000, s = 1.05, q = 20, vocabulary = "family" } }
```

The rank follows the [Zipf–Mandelbrot law][zm] with its usual parameters: rank $k$ has weight
$(k + q)^{-s}$. `q` defaults to 0, which is Zipf's law; `count` writes that many words.

Real names have a flat head and a steeper tail, which one Zipf exponent can't give: an `s` that
makes the tail right gives the first name far too many people. The offset `q` makes the first
ranks alike, and `s` sets the tail. The lines above are fitted to [US given names][ssa] of women
born 1967 to 2001, and to the [2010 US Census surnames][census]:

| Share of people | top name | top 10 | top 100 |
|---|---|---|---|
| given names | 2.1% | 12% | 46% |
| `n = 40_000, s = 1.1` (Zipf) | 10% | 33% | 57% |
| `n = 20_000, s = 1.4, q = 25` | 1.6% | 13% | 50% |
| surnames | 0.8% | 4.9% | 17% |
| `n = 100_000, s = 1.05, q = 20` | 0.7% | 5.6% | 24% |

How concentrated family names are differs most between countries: a smaller `q` or a larger `s`
gives the first names more people. The first name matters for performance, as a search for it
matches the most records.

[leis]: https://www.vldb.org/pvldb/vol9/p204-leis.pdf
[dsb]: https://www.vldb.org/pvldb/vol14/p3376-ding.pdf
[zm]: https://en.wikipedia.org/wiki/Zipf%E2%80%93Mandelbrot_law
[ssa]: https://www.ssa.gov/oact/babynames/limits.html
[census]: https://www.census.gov/topics/population/genealogy/data/2010_surnames.html
