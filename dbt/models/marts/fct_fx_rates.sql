{{
  config(
    materialized='table',
    table_type='iceberg',
    format='parquet'
  )
}}

with staged as (

    select * from {{ ref('stg_fx_rates') }}

),

deduplicated as (

    select
        trading_date,
        data_source,
        base_currency,
        target_currency,
        exchange_rate,
        row_number() over (
            partition by trading_date, base_currency, target_currency
            order by
                case data_source
                    when 'ecb' then 1
                    when 'frankfurter' then 2
                    else 3
                end
        ) as row_num
    from staged

)

select
    trading_date,
    data_source,
    base_currency,
    target_currency,
    exchange_rate,
    concat(base_currency, '/', target_currency) as currency_pair
from deduplicated
where row_num = 1
