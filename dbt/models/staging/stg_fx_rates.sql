with source as (

    select * from {{ source('fxlake', 'fx_rates') }}

)

select
    cast(date as date)          as trading_date,
    source                      as data_source,
    base_currency,
    target_currency,
    cast(rate as decimal(18,8)) as exchange_rate
from source
where date is not null
  and rate is not null
