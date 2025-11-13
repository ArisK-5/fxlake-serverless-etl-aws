#######################################
# SNS Topic and Subscription
#######################################

resource "aws_sns_topic" "alerts" {
  name = "fxlake-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.sns_email_address
}

#######################################
# Lambda Errors Alarm
#######################################

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "fxlake-lambda-errors"
  alarm_description   = "Triggered when Lambda function encounters errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.api_ingest.function_name
  }
}

#######################################
# Glue Job Failure Alarm
#######################################

resource "aws_cloudwatch_metric_alarm" "glue_job_failure" {
  alarm_name          = "fxlake-glue-job-failure"
  alarm_description   = "Triggered when Glue job tasks fail"
  namespace           = "AWS/Glue"
  metric_name         = "glue.driver.aggregate.numFailedTasks"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    JobName = aws_glue_job.transform.name
  }
}

#######################################
# Athena Query Failures & Empty Results Alarms
#######################################

resource "aws_cloudwatch_metric_alarm" "athena_query_failures" {
  alarm_name          = "fxlake-athena-query-failures"
  alarm_description   = "Triggered when Athena query executions fail"
  namespace           = "${var.metric_namespace_prefix}/Athena"
  metric_name         = "QueryFailed"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    WorkGroup = aws_athena_workgroup.fxlake.name
    Pipeline  = var.pipeline
  }
}

resource "aws_cloudwatch_metric_alarm" "athena_empty_results" {
  alarm_name          = "fxlake-athena-empty-results"
  alarm_description   = "Triggered when Athena query returns zero rows"
  namespace           = "${var.metric_namespace_prefix}/Athena"
  metric_name         = "EmptyQueryResults"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    Pipeline = var.pipeline
  }
}

#######################################
# Step Function Execution Failed & Throttling Alarms
#######################################

resource "aws_cloudwatch_metric_alarm" "step_function_execution_failed" {
  alarm_name          = "fxlake-stepfunctions-execution-failed"
  alarm_description   = "Triggered when Step Function execution fails"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.etl.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "step_function_throttles" {
  alarm_name          = "fxlake-stepfunctions-throttles"
  alarm_description   = "Triggered when Step Function throttling occurs"
  namespace           = "AWS/States"
  metric_name         = "ExecutionThrottled"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.etl.arn
  }
}

#######################################
# CloudTrail Unauthorized Access Alarm
#######################################

resource "aws_cloudwatch_metric_alarm" "unauthorized_api_alarm" {
  alarm_name          = "fxlake-unauthorized-api-calls"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  metric_name         = "UnauthorizedAPICallCount"
  namespace           = "CloudTrailMetrics"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Triggers if multiple unauthorized AWS API calls are detected"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

#######################################
# CloudWatch Dashboard
#######################################

locals {
  athena_namespace = "${var.metric_namespace_prefix}/Athena"
}

resource "aws_cloudwatch_dashboard" "fxlake_alarms_dashboard" {
  dashboard_name = "FXLake-Alarms-Dashboard"

  dashboard_body = jsonencode({
    widgets = [

      # Lambda Errors
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.api_ingest.function_name]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Lambda Errors (API Ingestion)"
          period = 60
          stat   = "Sum"
        }
      },

      # Glue Job Failed Tasks
      {
        type   = "metric"
        x      = 6
        y      = 0
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["AWS/Glue", "glue.driver.aggregate.numFailedTasks", "JobName", aws_glue_job.transform.name]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Glue Job Failed Tasks"
          period = 60
          stat   = "Sum"
        }
      },

      # Athena Query Failures
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 6
        height = 6
        properties = {
          metrics = [
            [local.athena_namespace, "QueryFailed", "WorkGroup", aws_athena_workgroup.fxlake.name]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Athena Query Failures"
          period = 60
          stat   = "Sum"
        }
      },

      # Athena Empty Results
      {
        type   = "metric"
        x      = 18
        y      = 0
        width  = 6
        height = 6
        properties = {
          metrics = [
            [local.athena_namespace, "EmptyQueryResults"]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Athena Empty Query Results"
          period = 60
          stat   = "Sum"
        }
      },

      # Step Function Execution Failures
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.etl.arn]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Step Function Executions Failed"
          period = 60
          stat   = "Sum"
        }
      },

      # Step Function Throttles
      {
        type   = "metric"
        x      = 6
        y      = 6
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["AWS/States", "ExecutionThrottled", "StateMachineArn", aws_sfn_state_machine.etl.arn]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Step Function Execution Throttled"
          period = 60
          stat   = "Sum"
        }
      },

      # CloudTrail Unauthorized API Calls
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["CloudTrailMetrics", "UnauthorizedAPICallCount"]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Unauthorized API Calls (CloudTrail)"
          period = 60
          stat   = "Sum"
        }
      },

      # SNS Notifications Delivered
      {
        type   = "metric"
        x      = 18
        y      = 6
        width  = 6
        height = 6
        properties = {
          metrics = [
            ["AWS/SNS", "NumberOfNotificationsDelivered", "TopicName", aws_sns_topic.alerts.name]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "SNS Notifications Delivered"
          period = 60
          stat   = "Sum"
        }
      }
    ]
  })
}
