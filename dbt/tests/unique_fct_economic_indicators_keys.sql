select
    observation_date,
    series_id,
    count(*) as row_count
from {{ ref('fct_economic_indicators') }}
group by observation_date, series_id
having count(*) > 1
