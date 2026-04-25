{{
  config(
    materialized='table',
    table_type='iceberg',
    format='parquet'
  )
}}

with staged as (

    select * from {{ ref('stg_economic_indicators') }}

),

deduplicated as (

    select
        observation_date,
        data_source,
        series_id,
        observation_value,
        row_number() over (
            partition by observation_date, series_id
            order by observation_date
        ) as row_num
    from staged

)

select
    observation_date,
    data_source,
    series_id,
    observation_value
from deduplicated
where row_num = 1
