select
    trading_date,
    base_currency,
    target_currency,
    count(*) as row_count
from {{ ref('fct_fx_rates') }}
group by trading_date, base_currency, target_currency
having count(*) > 1
