{% test positive_values(model, column_name) %}
{# Replaces quality.py check_positive_values — all values must be > 0 #}

select {{ column_name }}
from {{ model }}
where {{ column_name }} <= 0

{% endtest %}
