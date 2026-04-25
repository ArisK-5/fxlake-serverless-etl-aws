{% test rate_in_range(model, column_name, min_val=0.0001, max_val=1000) %}
{# Replaces quality.py check_rate_range — values must be within [min_val, max_val] #}

select {{ column_name }}
from {{ model }}
where {{ column_name }} < {{ min_val }}
   or {{ column_name }} > {{ max_val }}

{% endtest %}
