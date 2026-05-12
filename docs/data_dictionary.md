# FXLake Data Dictionary

This document describes all tables, columns, and data contracts in the FXLake data lake. Cross-referenced with dbt schema definitions in `dbt/models/`.

## Database

| Property | Value |
|----------|-------|
| **Name** | `fxlake` |
| **Engine** | AWS Athena (engine v3) |
| **Catalog** | AWS Glue Data Catalog |
| **Table format** | Apache Iceberg v2 |
| **Owner** | `fxlake-pipeline` |
| **Classification** | `financial-data` |

---

## Raw Tables (Iceberg)

These tables are written by the Iceberg writer Lambda after quality checks pass. They serve as the source of truth for all downstream transformations.

### `fx_rates`

Daily foreign exchange rates from Frankfurter API and ECB Statistics Data Warehouse.

| Property | Value |
|----------|-------|
| **Update frequency** | Daily |
| **Sources** | Frankfurter API, ECB SDW |
| **Retention** | 365 days |
| **S3 location** | `s3://<processed_bucket>/iceberg/fx_rates/` |
| **Quality checks** | `common/quality.py:run_fx_checks()` |
| **dbt source** | `dbt/models/staging/src_fxlake.yml` |

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `date` | `string` | No | Trading date in ISO 8601 format (YYYY-MM-DD) |
| `source` | `string` | No | Data provider identifier: `frankfurter` or `ecb` |
| `base_currency` | `string` | No | ISO 4217 base currency code (e.g. `EUR`) |
| `target_currency` | `string` | No | ISO 4217 target currency code (e.g. `USD`, `GBP`) |
| `rate` | `double` | No | Exchange rate: 1 unit of base_currency = rate units of target_currency |

**Quality constraints (enforced pre-INSERT):**

| Check | Level | Rule |
|-------|-------|------|
| Required columns | CRITICAL | All 5 columns present |
| No nulls | CRITICAL | `date` and `rate` must not be null |
| Positive values | CRITICAL | `rate > 0` |
| Rate range | WARNING | `0.0001 <= rate <= 1000` |
| Valid source | WARNING | `source` in `{frankfurter, ecb}` |
| No duplicates | WARNING | Unique `(date, target_currency)` |

### `economic_indicators`

Economic indicator observations from FRED (Federal Reserve Economic Data).

| Property | Value |
|----------|-------|
| **Update frequency** | Daily |
| **Sources** | FRED API |
| **Retention** | 365 days |
| **S3 location** | `s3://<processed_bucket>/iceberg/economic_indicators/` |
| **Quality checks** | `common/quality.py:run_economic_checks()` |
| **dbt source** | `dbt/models/staging/src_fxlake.yml` |

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `date` | `string` | No | Observation date in ISO 8601 format (YYYY-MM-DD) |
| `source` | `string` | No | Data provider identifier: `fred` |
| `series_id` | `string` | No | FRED series identifier (e.g. `UNRATE` for unemployment rate) |
| `value` | `double` | No | Observation value for the given series and date |

**Quality constraints (enforced pre-INSERT):**

| Check | Level | Rule |
|-------|-------|------|
| Required columns | CRITICAL | All 4 columns present |
| No nulls | CRITICAL | `date` and `value` must not be null |
| No duplicates | WARNING | Unique `(date, series_id)` |

---

## Staging Views (dbt)

Staging models are materialised as views. They apply type casting, renaming, filtering, and deduplication on top of raw Iceberg tables. Schema defined in `dbt/models/staging/schema.yml`.

### `stg_fx_rates`

| Column | Type | Source column | Description |
|--------|------|---------------|-------------|
| `trading_date` | `date` | `date` | Trading date, cast from string to date |
| `data_source` | `varchar` | `source` | Upstream data provider (`frankfurter` or `ecb`) |
| `base_currency` | `varchar` | `base_currency` | ISO 4217 base currency code |
| `target_currency` | `varchar` | `target_currency` | ISO 4217 target currency code |
| `exchange_rate` | `decimal(18,8)` | `rate` | Exchange rate value |

**Deduplication:** `ROW_NUMBER()` over `(trading_date, base_currency, target_currency)` ordered by source priority: ECB > Frankfurter.

**dbt tests:** `not_null` on all columns, `positive_values` on `exchange_rate`, `accepted_values` on `data_source`, `unique_combination_of_columns` on `(trading_date, base_currency, target_currency)`.

### `stg_economic_indicators`

| Column | Type | Source column | Description |
|--------|------|---------------|-------------|
| `observation_date` | `date` | `date` | Observation date, cast from string to date |
| `data_source` | `varchar` | `source` | Upstream data provider (`fred`) |
| `series_id` | `varchar` | `series_id` | FRED series identifier |
| `observation_value` | `decimal(18,4)` | `value` | Observation value |

**Deduplication:** `ROW_NUMBER()` over `(observation_date, series_id)`.

**dbt tests:** `not_null` on all columns, `positive_values` on `observation_value`, `unique_combination_of_columns` on `(observation_date, series_id)`.

---

## Mart Tables (dbt, Iceberg)

Mart models are materialised as Iceberg tables. They add derived columns on top of staging views. Schema defined in `dbt/models/marts/schema.yml`.

### `fct_fx_rates`

| Column | Type | Description |
|--------|------|-------------|
| `trading_date` | `date` | Trading date |
| `data_source` | `varchar` | Winning data provider after deduplication |
| `base_currency` | `varchar` | ISO 4217 base currency code |
| `target_currency` | `varchar` | ISO 4217 target currency code |
| `exchange_rate` | `decimal(18,8)` | Exchange rate value |
| `currency_pair` | `varchar` | Derived column: `base_currency || '/' || target_currency` (e.g. `EUR/USD`) |

### `fct_economic_indicators`

| Column | Type | Description |
|--------|------|-------------|
| `observation_date` | `date` | Observation date |
| `data_source` | `varchar` | Data provider |
| `series_id` | `varchar` | FRED series identifier |
| `observation_value` | `decimal(18,4)` | Observation value |

---

## Example Queries

### Latest rates for all currency pairs

```sql
SELECT trading_date, currency_pair, exchange_rate
FROM fct_fx_rates
WHERE trading_date = (SELECT MAX(trading_date) FROM fct_fx_rates)
ORDER BY currency_pair;
```

### Data freshness per source

```sql
SELECT source, MAX(date) AS latest_date, COUNT(*) AS total_records
FROM fx_rates
GROUP BY source;
```

### Economic indicator time series

```sql
SELECT observation_date, series_id, observation_value
FROM fct_economic_indicators
WHERE series_id = 'UNRATE'
ORDER BY observation_date DESC
LIMIT 12;
```

### Cross-source rate comparison

```sql
SELECT a.date, a.target_currency,
       a.rate AS frankfurter_rate,
       b.rate AS ecb_rate,
       ABS(a.rate - b.rate) / a.rate * 100 AS pct_diff
FROM fx_rates a
JOIN fx_rates b
  ON a.date = b.date AND a.target_currency = b.target_currency
WHERE a.source = 'frankfurter' AND b.source = 'ecb'
  AND ABS(a.rate - b.rate) / a.rate > 0.01
ORDER BY pct_diff DESC;
```

---

## S3 Layout

| Bucket | Path pattern | Contents |
|--------|-------------|----------|
| Raw | `exchange_rates_{BASE}_{START}_to_{END}.json` | Frankfurter API responses |
| Raw | `ecb_rates_{START}_to_{END}.json` | ECB SDW SDMX-JSON responses |
| Raw | `fred_{series}_{START}_to_{END}.json` | FRED API responses |
| Processed | `iceberg/fx_rates/` | Iceberg data + metadata files |
| Processed | `iceberg/economic_indicators/` | Iceberg data + metadata files |
| Processed | `fx_rates/quality_reports/{stem}_quality.json` | Quality check reports |
| Processed | `economic_indicators/quality_reports/{stem}_quality.json` | Quality check reports |
| Quarantine | `fx_rates/quarantine/{stem}.json` | Records failing CRITICAL quality checks |
| Quarantine | `economic_indicators/quarantine/{stem}.json` | Records failing CRITICAL quality checks |
| Athena results | `results/` | Query results (1-day TTL) |

---

## Data Lineage

```
Frankfurter API ──┐
                  ├─► Raw S3 ──► quality.py ──► fx_rates (Iceberg)
ECB SDW API ──────┘                                  │
                                                     ▼
                                              stg_fx_rates (view)
                                                     │
                                                     ▼
                                              fct_fx_rates (Iceberg)

FRED API ──────────► Raw S3 ──► quality.py ──► economic_indicators (Iceberg)
                                                     │
                                                     ▼
                                              stg_economic_indicators (view)
                                                     │
                                                     ▼
                                              fct_economic_indicators (Iceberg)
```
