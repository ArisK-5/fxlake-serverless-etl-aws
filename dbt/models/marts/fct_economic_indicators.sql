{{
  config(
    materialized='table',
    table_type='iceberg',
    format='parquet'
  )
}}

select
    observation_date,
    data_source,
    series_id,
    observation_value
from {{ ref('stg_economic_indicators') }}
