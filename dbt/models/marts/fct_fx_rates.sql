{{
  config(
    materialized='table',
    table_type='iceberg',
    format='parquet'
  )
}}

select
    trading_date,
    data_source,
    base_currency,
    target_currency,
    exchange_rate,
    concat(base_currency, '/', target_currency) as currency_pair
from {{ ref('stg_fx_rates') }}
