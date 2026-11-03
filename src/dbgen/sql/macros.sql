-- The draws every stage that draws uses (orgunits, generate), as DuckDB macros.
--
-- A draw is a hash of the seed, a key and a label: a build is reproducible, independent of row
-- order and threads, and adding a draw shifts no other. The key names what is drawn for (a
-- facility, a tracked entity, an event), the label the quantity, the field's path in the spec. The
-- same key and label give the same draw wherever they appear; a suffix to the label (#a, #b)
-- derives another, independent one. The draws of a spec's expressions, u('x') and n('x'), append
-- @x instead (../expr.py), so they never meet these.
--
-- hash() is DuckDB's: 1.3 changed it, and with it every draw, so pyproject.toml pins DuckDB and
-- the manifest records its version. A hash no version can change costs more: md5_number_lower 23
-- times as much, splitmix64 in HUGEINT arithmetic 150 times (20M draws, DuckDB 1.5.6).
--
-- DuckDB combines the hashes of several values so that draws of one key under different labels
-- are correlated (-0.98 between two labels), so the combined hash is hashed once
-- more.
create or replace macro draw(key, label) as hash(hash(getvariable('seed'), key, label));

-- Uniform in [0, 1): the top 53 bits of the draw divided by 2^53. A double holds 53 bits, so the
-- whole draw divided by 2^64 rounds up to 1.0 near 2^64, where ln(1 - u) is -inf.
create or replace macro rand_u(key, label) as (draw(key, label) >> 11)::double / 9007199254740992.0;

-- Standard normal by Box-Muller from two uniforms.
create or replace macro rand_n(key, label) as
    sqrt(-2 * ln(1 - rand_u(key, label || '#a'))) * cos(2 * pi() * rand_u(key, label || '#b'));

-- The normal distribution's CDF and inverse, for truncated normal draws. A macro repeats its
-- arguments wherever they appear, so nested macros grow into expressions that take DuckDB seconds
-- to plan; list_transform([v], lambda x: ...)[1] binds a value once instead.

-- Standard normal CDF, from the complementary error function of Numerical Recipes (erfcc,
-- fractional error below 1.2e-7 everywhere, so the tails stay accurate).
create or replace macro erfc_nr(z) as
    list_transform([1 / (1 + 0.5 * z)], lambda t:
        t * exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418
            + t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587
            + t * (-0.82215223 + t * 0.17087277))))))))))[1];
create or replace macro norm_cdf(x) as
    list_transform([x / sqrt(2)], lambda z:
        case when z >= 0 then 1 - 0.5 * erfc_nr(z) else 0.5 * erfc_nr(-z) end)[1];

-- Inverse of the standard normal CDF for p in (0, 1), by Acklam's rational approximation
-- (relative error below 1.15e-9). The tail is the lower one at q = sqrt(-2 ln p).
create or replace macro norm_ppf_tail(q) as
    (((((-7.784894002430293e-03 * q - 3.223964580411365e-01) * q - 2.400758277161838e+00) * q
        - 2.549732539343734e+00) * q + 4.374664141464968e+00) * q + 2.938163982698783e+00)
    / ((((7.784695709041462e-03 * q + 3.224671290700398e-01) * q + 2.445134137142996e+00) * q
        + 3.754408661907416e+00) * q + 1);
create or replace macro norm_ppf_central(q, r) as
    (((((-3.969683028665376e+01 * r + 2.209460984245205e+02) * r - 2.759285104469687e+02) * r
        + 1.383577518672690e+02) * r - 3.066479806614716e+01) * r + 2.506628277459239e+00) * q
    / (((((-5.447609879822406e+01 * r + 1.615858368580409e+02) * r - 1.556989798598866e+02) * r
        + 6.680131188771972e+01) * r - 1.328068155288572e+01) * r + 1);
create or replace macro norm_ppf(p) as
    list_transform([p], lambda u:
        case when u < 0.02425 then
                  list_transform([sqrt(-2 * ln(u))], lambda q: norm_ppf_tail(q))[1]
             when u > 1 - 0.02425 then
                  list_transform([sqrt(-2 * ln(1 - u))], lambda q: -norm_ppf_tail(q))[1]
             else list_transform([u - 0.5], lambda q: norm_ppf_central(q, q * q))[1]
        end)[1];

-- A normal draw truncated to [low, high] by the inverse CDF of one uniform: truncating moves the
-- tails inside the bounds, where clipping would pile them up on a bound.
create or replace macro rand_tn(key, label, mean, sd, low, high) as
    list_transform([norm_cdf((low - mean) / sd)], lambda a:
        list_transform([norm_cdf((high - mean) / sd)], lambda b:
            least(greatest(mean + sd * norm_ppf(
                least(greatest(a + rand_u(key, label) * (b - a), 1e-300), 1 - 1e-16)),
                low), high))[1])[1];

-- A Poisson draw of mean lam by inverting its CDF: the count of k whose cumulative probability is
-- below a uniform, summed in one pass over k with each probability from the one before. That takes
-- lam + 7 sqrt(lam) + 10 steps a draw, and its first probability, exp(-lam), is 0 as a double above
-- a lam of about 745. So a mean above 50 is drawn from a normal of the same mean and variance,
-- rounded, which is close at that size.
create or replace macro rand_poisson(key, label, lam) as
    list_transform([{'lam': greatest(coalesce(lam::double, 0), 0), 'u': rand_u(key, label)}],
        lambda p: case
            when p.lam = 0 then 0
            when p.lam > 50 then greatest(0, round(p.lam + sqrt(p.lam) * rand_n(key, label || '#n')))::bigint
            else list_reduce(
                list_transform(range(1, ceil(p.lam + 7 * sqrt(p.lam) + 10)::bigint),
                    lambda k: {'k': k::double, 'p': 0::double, 'c': 0::double, 'n': 0::double}),
                lambda acc, x: {'k': x.k, 'p': acc.p * p.lam / x.k, 'c': acc.c + acc.p * p.lam / x.k,
                    'n': acc.n + case when acc.c + acc.p * p.lam / x.k < p.u then 1 else 0 end},
                {'k': 0::double, 'p': exp(-p.lam), 'c': exp(-p.lam),
                 'n': case when exp(-p.lam) < p.u then 1::double else 0::double end}
            ).n::bigint
        end)[1];

-- A rank from 1 to n of a Zipf law of exponent s: rank k has weight k^-s. It inverts the CDF of
-- the law as a continuous one, and floors the result.
create or replace macro rand_zipf(key, label, n, s) as
    list_transform([{'u': rand_u(key, label), 'n': n::double, 's': s::double}], lambda z:
        least(z.n, greatest(1, floor(case
            when abs(z.s - 1) < 1e-9 then exp(z.u * ln(z.n + 1))
            else pow(1 + z.u * (pow(z.n + 1, 1 - z.s) - 1), 1 / (1 - z.s))
        end)))::bigint)[1];

-- One of 90 syllables, for i from 1 to 90: a consonant and a vowel.
create or replace macro syllable(i) as
    substr('bdfghjklmnprstvwyz', (i - 1) // 5 + 1, 1) || substr('aeiou', (i - 1) % 5 + 1, 1);

-- A made-up word of 2 to 4 syllables and a final k, s, t or x for a rank of a vocabulary, the same
-- for every record of the rank.
create or replace macro word(vocabulary, rank) as
    list_transform([hash(getvariable('seed'), vocabulary, rank)], lambda h:
        upper(syllable(1 + (h % 90)::bigint)[1]) || syllable(1 + (h % 90)::bigint)[2]
        || syllable(1 + ((h // 90) % 90)::bigint)
        || case when (h // 8100) % 3 > 0 then syllable(1 + ((h // 24300) % 90)::bigint) else '' end
        || case when (h // 8100) % 3 > 1 then syllable(1 + ((h // 2187000) % 90)::bigint) else '' end
        || substr('kstx', 1 + ((h // 196830000) % 4)::bigint, 1)
    )[1];

-- A date as days since 1970-01-01, the unit of the spec's dates, and back.
create or replace macro to_days(d) as (d::date - date '1970-01-01')::double;
create or replace macro of_days(x) as date '1970-01-01' + floor(x)::integer;
