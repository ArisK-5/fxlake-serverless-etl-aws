with source as (

    select * from {{ source('fxlake', 'fx_rates') }}

),

cleaned as (

    select
        cast(date as date)          as trading_date,
        source                      as data_source,
        base_currency,
        target_currency,
        cast(rate as decimal(18,8)) as exchange_rate
    from source
    where date is not null
      and rate is not null
      and cast(rate as decimal(18,8)) > 0

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
    from cleaned

)

select
    trading_date,
    data_source,
    base_currency,
    target_currency,
    exchange_rate
from deduplicated
where row_num = 1
