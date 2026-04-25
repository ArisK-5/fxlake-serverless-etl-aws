with source as (

    select * from {{ source('fxlake', 'economic_indicators') }}

)

select
    cast(date as date)           as observation_date,
    source                       as data_source,
    series_id,
    cast(value as decimal(18,4)) as observation_value
from source
where date is not null
  and value is not null
