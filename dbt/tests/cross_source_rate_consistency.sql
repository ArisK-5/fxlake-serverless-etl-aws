select
    a.trading_date,
    a.target_currency,
    a.exchange_rate as frankfurter_rate,
    b.exchange_rate as ecb_rate,
    abs(a.exchange_rate - b.exchange_rate) / nullif(b.exchange_rate, 0) as deviation
from {{ ref('fct_fx_rates') }} a
join {{ ref('fct_fx_rates') }} b
    on a.trading_date = b.trading_date
    and a.target_currency = b.target_currency
where a.data_source = 'frankfurter'
    and b.data_source = 'ecb'
    and abs(a.exchange_rate - b.exchange_rate) / nullif(b.exchange_rate, 0) > 0.01
