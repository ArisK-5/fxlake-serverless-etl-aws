resource "aws_sns_topic" "alerts" {
  name = "fxlake-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.sns_email_address
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "fxlake-lambda-errors"
  alarm_description   = "This metric monitors lambda function errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "Errors"
  namespace           = "FXLake/Lambda"
  period              = "60"
  statistic           = "Sum"
  threshold           = "0"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.api_ingest.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "glue_job_failure" {
  alarm_name          = "fxlake-glue-job-failure"
  alarm_description   = "This metric monitors Glue job failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "glue.driver.aggregate.numFailedTasks"
  namespace           = "FXLake/Glue"
  period              = "60"
  statistic           = "Sum"
  threshold           = "0"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    JobName = aws_glue_job.transform.name
  }
}

resource "aws_cloudwatch_metric_alarm" "athena_query_failures" {
  alarm_name          = "fxlake-athena-query-failures"
  alarm_description   = "Triggered when Athena query executions fail"
  namespace           = "FXLake/Athena"
  metric_name         = "QueryFailed"
  statistic           = "Sum"
  period              = "60"
  evaluation_periods  = "2"
  threshold           = "0"
  comparison_operator = "GreaterThanOrEqualToThreshold"

  dimensions = {
    WorkGroup = aws_athena_workgroup.fxlake.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "athena_empty_results" {
  alarm_name          = "fxlake-athena-empty-results"
  alarm_description   = "Triggered when Athena query returns zero rows"
  namespace           = "FXLake/Athena"
  metric_name         = "EmptyQueryResults"
  statistic           = "Sum"
  period              = "60"
  evaluation_periods  = "2"
  threshold           = "0"
  comparison_operator = "GreaterThanOrEqualToThreshold"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "step_function_execution_failed" {
  alarm_name          = "fxlake-stepfunctions-execution-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "ExecutionsFailed"
  namespace           = "FXLake/States"
  period              = "60"
  statistic           = "Sum"
  threshold           = "0"
  treat_missing_data  = "notBreaching"
  alarm_description   = "This metric monitors Step Functions execution failures"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.etl.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "step_function_throttles" {
  alarm_name          = "fxlake-stepfunctions-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "ExecutionThrottled"
  namespace           = "FXLake/States"
  period              = "300"
  statistic           = "Sum"
  threshold           = "0"
  alarm_description   = "This metric monitors Step Function throttling"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.etl.arn
  }
}
