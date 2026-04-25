{% macro generate_quality_report(model_name, domain) %}
{#
    Logs the mapping between quality.py check names and dbt test names.

    Usage:
      dbt run-operation generate_quality_report --args '{"model_name": "stg_fx_rates", "domain": "fx_rates"}'
#}

{% set quality_checks = {
    'stg_fx_rates': [
        {'check_name': 'not_null_trading_date',     'level': 'CRITICAL', 'dbt_test': 'not_null_stg_fx_rates_trading_date'},
        {'check_name': 'not_null_exchange_rate',     'level': 'CRITICAL', 'dbt_test': 'not_null_stg_fx_rates_exchange_rate'},
        {'check_name': 'positive_exchange_rate',     'level': 'CRITICAL', 'dbt_test': 'positive_values_stg_fx_rates_exchange_rate'},
        {'check_name': 'range_exchange_rate',        'level': 'WARNING',  'dbt_test': 'rate_in_range_stg_fx_rates_exchange_rate'},
        {'check_name': 'value_set_data_source',      'level': 'WARNING',  'dbt_test': 'accepted_values_stg_fx_rates_data_source'},
        {'check_name': 'no_duplicate_date_currency', 'level': 'WARNING',  'dbt_test': 'dbt_utils_unique_combination_of_columns_stg_fx_rates'},
    ],
    'stg_economic_indicators': [
        {'check_name': 'not_null_observation_date',  'level': 'CRITICAL', 'dbt_test': 'not_null_stg_economic_indicators_observation_date'},
        {'check_name': 'not_null_observation_value', 'level': 'CRITICAL', 'dbt_test': 'not_null_stg_economic_indicators_observation_value'},
        {'check_name': 'positive_observation_value', 'level': 'CRITICAL', 'dbt_test': 'positive_values_stg_economic_indicators_observation_value'},
        {'check_name': 'no_duplicate_date_series',   'level': 'WARNING',  'dbt_test': 'dbt_utils_unique_combination_of_columns_stg_economic_indicators'},
    ],
} %}

{% if model_name not in quality_checks %}
    {{ exceptions.raise_compiler_error("Unknown model: " ~ model_name ~ ". Supported: " ~ quality_checks.keys() | join(', ')) }}
{% endif %}

{{ log("Quality report for " ~ model_name ~ " (" ~ domain ~ "):", info=true) }}
{{ log("  Check mapping (quality.py → dbt test):", info=true) }}

{% for check in quality_checks[model_name] %}
    {{ log("  - " ~ check.check_name ~ " → " ~ check.dbt_test ~ " [" ~ check.level ~ "]", info=true) }}
{% endfor %}

{{ log("", info=true) }}
{{ log("  Run 'dbt test --select " ~ model_name ~ "' to execute all quality checks.", info=true) }}
{{ log("  Note: check_required_columns is enforced upstream by Iceberg schema — columns", info=true) }}
{{ log("  that don't exist on the source table will cause a compile-time error in dbt.", info=true) }}

{% endmacro %}
