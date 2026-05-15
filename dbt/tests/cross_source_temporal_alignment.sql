with source_ranges as (
    select
        source as data_source,
        min(cast(date as date)) as min_date,
        max(cast(date as date)) as max_date
    from {{ source('fxlake', 'fx_rates') }}
    group by source
),

date_gaps as (
    select
        a.data_source as source_a,
        b.data_source as source_b,
        abs(date_diff('day', a.max_date, b.max_date)) as max_date_gap_days
    from source_ranges a
    cross join source_ranges b
    where a.data_source < b.data_source
)

select *
from date_gaps
where max_date_gap_days > 1
