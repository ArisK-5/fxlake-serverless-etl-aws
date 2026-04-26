{% docs stg_fx_rates_dedup %}
**Deduplication strategy:** When both ECB and Frankfurter provide a rate for the
same (date, base, target) triple, ECB wins. This is implemented via
`ROW_NUMBER()` with a `CASE` ordering that assigns ECB priority 1, Frankfurter
priority 2. Only the top-ranked row per group is kept.

Rows with null dates, null rates, or non-positive rates are filtered before
deduplication so they never compete with valid rows.
{% enddocs %}

{% docs stg_economic_indicators_dedup %}
**Deduplication strategy:** When duplicate observations exist for the same
(date, series_id) pair, the first row by `data_source` alphabetical order wins.
Currently only FRED feeds this table, so duplicates come from overlapping
backfill windows — not from competing sources.

Sentinel values (`"."`) used by FRED for unreleased data are excluded in the
cleaning step before deduplication.
{% enddocs %}

{% docs fct_fx_rates_currency_pair %}
Derived column that concatenates `base_currency` and `target_currency` with a
`/` separator (e.g. `EUR/USD`). Provides a human-readable identifier commonly
used in FX trading screens and reports.
{% enddocs %}
