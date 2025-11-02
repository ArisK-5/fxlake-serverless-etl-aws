import boto3

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")


def lambda_handler(event, context):
    query_execution_id = event.get("QueryExecutionId")
    if not query_execution_id:
        raise ValueError("Missing QueryExecutionId")

    # Get the query results
    response = athena.get_query_results(QueryExecutionId=query_execution_id)
    rows = len(response.get("ResultSet", {}).get("Rows", [])) - 1  # subtract header

    # Publish metric
    cloudwatch.put_metric_data(
        Namespace="AWS/Athena",
        MetricData=[
            {
                "MetricName": "EmptyQueryResults",
                "Value": 1 if rows == 0 else 0,
                "Unit": "Count",
            }
        ],
    )

    print(f"Athena query returned {rows} rows.")
    return {"rows": rows}
