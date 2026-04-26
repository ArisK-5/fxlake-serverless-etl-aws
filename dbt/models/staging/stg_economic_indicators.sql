with source as (

    select * from {{ source('fxlake', 'economic_indicators') }}

),

cleaned as (

    select
        cast(date as date)           as observation_date,
        source                       as data_source,
        series_id,
        cast(value as decimal(18,4)) as observation_value
    from source
    where date is not null
      and value is not null
      and cast(value as varchar) <> '.'

),

deduplicated as (

    select
        observation_date,
        data_source,
        series_id,
        observation_value,
        row_number() over (
            partition by observation_date, series_id
            order by data_source
        ) as row_num
    from cleaned

)

select
    observation_date,
    data_source,
    series_id,
    observation_value
from deduplicated
where row_num = 1
