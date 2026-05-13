#######################################
# SNS Topic and Subscription
#######################################

resource "aws_sns_topic" "alerts" {
  name = "fxlake-alerts"

  tags = {
    component = "monitoring"
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowBudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.alerts.arn
      }
    ]
  })
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
    FunctionName = aws_lambda_function.fx_ingest.function_name
  }
}

#######################################
# dbt Transform (CodeBuild) Failure Alarm
#######################################

resource "aws_cloudwatch_metric_alarm" "dbt_transform_failure" {
  alarm_name          = "fxlake-dbt-transform-failure"
  alarm_description   = "Triggered when dbt CodeBuild transform job fails"
  namespace           = "AWS/CodeBuild"
  metric_name         = "FailedBuilds"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    ProjectName = aws_codebuild_project.dbt_transform.name
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
# Data Quality Alarms
#######################################

resource "aws_cloudwatch_metric_alarm" "data_quality_checks_failed" {
  alarm_name          = "fxlake-data-quality-checks-failed"
  alarm_description   = "Triggered when any data quality check fails during Glue transform"
  namespace           = "${var.metric_namespace_prefix}/Quality"
  metric_name         = "DataQualityChecksFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    Domain = "fx_rates"
  }
}

resource "aws_cloudwatch_metric_alarm" "data_quality_checks_failed_econ" {
  alarm_name          = "fxlake-data-quality-checks-failed-econ"
  alarm_description   = "Triggered when any economic indicators quality check fails"
  namespace           = "${var.metric_namespace_prefix}/Quality"
  metric_name         = "DataQualityChecksFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    Domain = "economic_indicators"
  }
}

resource "aws_cloudwatch_metric_alarm" "stale_fx_data" {
  alarm_name          = "fxlake-stale-fx-data"
  alarm_description   = "Triggered when FX data is older than freshness threshold (>2 days)"
  namespace           = "${var.metric_namespace_prefix}/Athena"
  metric_name         = "StaleFXData"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "records_quarantined" {
  alarm_name          = "fxlake-records-quarantined"
  alarm_description   = "Triggered when records are quarantined due to CRITICAL quality failures"
  namespace           = "${var.metric_namespace_prefix}/Quality"
  metric_name         = "RecordsQuarantined"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

#######################################
# Iceberg Maintenance Job Failed Alarm
#######################################

resource "aws_cloudwatch_metric_alarm" "maintenance_job_failed" {
  alarm_name          = "fxlake-maintenance-job-failed"
  alarm_description   = "Triggered when Iceberg maintenance operations fail"
  namespace           = "${var.metric_namespace_prefix}/Maintenance"
  metric_name         = "MaintenanceJobFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = {
    component = "monitoring"
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
# Composite Alarm — Pipeline SLA
#######################################

resource "aws_cloudwatch_composite_alarm" "pipeline_sla" {
  alarm_name        = "fxlake-pipeline-sla"
  alarm_description = "Pipeline SLA breach: triggers when execution fails, data goes stale, or CRITICAL quality checks fail"

  alarm_rule = "ALARM(${aws_cloudwatch_metric_alarm.step_function_execution_failed.alarm_name}) OR ALARM(${aws_cloudwatch_metric_alarm.stale_fx_data.alarm_name}) OR ALARM(${aws_cloudwatch_metric_alarm.data_quality_checks_failed.alarm_name}) OR ALARM(${aws_cloudwatch_metric_alarm.dbt_transform_failure.alarm_name})"

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = {
    component = "monitoring"
  }
}

#######################################
# CloudWatch Dashboard
#######################################

locals {
  athena_namespace      = "${var.metric_namespace_prefix}/Athena"
  quality_namespace     = "${var.metric_namespace_prefix}/Quality"
  sla_namespace         = "${var.metric_namespace_prefix}/SLA"
  maintenance_namespace = "${var.metric_namespace_prefix}/Maintenance"
}

resource "aws_cloudwatch_dashboard" "fxlake_alarms_dashboard" {
  dashboard_name = "FXLake-Alarms-Dashboard"

  dashboard_body = jsonencode({
    widgets = [

      ###################################
      # Row 1 — Pipeline Health (y=0)
      ###################################

      # Step Function execution duration (p50, p90, p99) — 30-day time series
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/States", "ExecutionTime", "StateMachineArn", aws_sfn_state_machine.etl.arn, { stat = "p50", label = "p50" }],
            ["...", { stat = "p90", label = "p90" }],
            ["...", { stat = "p99", label = "p99" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Pipeline Execution Duration"
          period  = 86400
          yAxis   = { left = { label = "ms" } }
        }
      },

      # Lambda invocation count by function — 30-day time series
      {
        type   = "metric"
        x      = 8
        y      = 0
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.fx_ingest.function_name, { label = "FX" }],
            ["...", module.ecb_ingest.function_name, { label = "ECB" }],
            ["...", module.fred_ingest.function_name, { label = "FRED" }],
            ["...", aws_lambda_function.check_query_results.function_name, { label = "Validation" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Lambda Invocations by Function"
          period  = 86400
          stat    = "Sum"
        }
      },

      # Pipeline execution results — stat (succeeded, failed, throttled)
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", aws_sfn_state_machine.etl.arn, { label = "Succeeded" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.etl.arn, { label = "Failed" }],
            ["AWS/States", "ExecutionThrottled", "StateMachineArn", aws_sfn_state_machine.etl.arn, { label = "Throttled" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Pipeline Status (Last 24h)"
          period = 86400
          stat   = "Sum"
        }
      },

      ###################################
      # Row 2 — Data Quality (y=7)
      ###################################

      # Quality check failures by domain — 30-day time series
      {
        type   = "metric"
        x      = 0
        y      = 7
        width  = 8
        height = 6
        properties = {
          metrics = [
            [local.quality_namespace, "DataQualityChecksFailed", "Domain", "fx_rates", { label = "FX Rates" }],
            [local.quality_namespace, "DataQualityChecksFailed", "Domain", "economic_indicators", { label = "Economic" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Quality Check Failures by Domain"
          period  = 86400
          stat    = "Sum"
        }
      },

      # Records quarantined — last 24h stat
      {
        type   = "metric"
        x      = 8
        y      = 7
        width  = 8
        height = 6
        properties = {
          metrics = [
            [local.quality_namespace, "RecordsQuarantined", { label = "Quarantined" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Records Quarantined (Last 24h)"
          period = 86400
          stat   = "Sum"
        }
      },

      # Quality failures — time series showing quarantine trend
      {
        type   = "metric"
        x      = 16
        y      = 7
        width  = 8
        height = 6
        properties = {
          metrics = [
            [local.quality_namespace, "RecordsQuarantined", { label = "Quarantined" }],
            [local.quality_namespace, "DataQualityChecksFailed", "Domain", "fx_rates", { label = "FX Failures" }],
            [local.quality_namespace, "DataQualityChecksFailed", "Domain", "economic_indicators", { label = "Econ Failures" }]
          ]
          view    = "timeSeries"
          stacked = true
          region  = var.aws_region
          title   = "Quality Events (30-day trend)"
          period  = 86400
          stat    = "Sum"
        }
      },

      ###################################
      # Row 3 — Data Freshness (y=14)
      ###################################

      # Stale FX data — 30-day time series
      {
        type   = "metric"
        x      = 0
        y      = 14
        width  = 8
        height = 6
        properties = {
          metrics = [
            [local.athena_namespace, "StaleFXData", { label = "Stale Data Events" }],
            [local.athena_namespace, "EmptyQueryResults", { label = "Empty Results" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Data Freshness Issues"
          period  = 86400
          stat    = "Sum"
        }
      },

      # Ingestion latency per source (Lambda duration as proxy) — 30-day time series
      {
        type   = "metric"
        x      = 8
        y      = 14
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.fx_ingest.function_name, { label = "FX", stat = "p90" }],
            ["...", module.ecb_ingest.function_name, { label = "ECB", stat = "p90" }],
            ["...", module.fred_ingest.function_name, { label = "FRED", stat = "p90" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Ingestion Latency per Source (p90)"
          period  = 86400
          yAxis   = { left = { label = "ms" } }
        }
      },

      # Current freshness stats
      {
        type   = "metric"
        x      = 16
        y      = 14
        width  = 8
        height = 6
        properties = {
          metrics = [
            [local.athena_namespace, "StaleFXData", { label = "Stale FX" }],
            [local.athena_namespace, "EmptyQueryResults", { label = "Empty Results" }],
            [local.athena_namespace, "QueryFailed", "WorkGroup", aws_athena_workgroup.fxlake.name, { label = "Query Failures" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Current Freshness & Query Status"
          period = 86400
          stat   = "Sum"
        }
      },

      ###################################
      # Row 4 — Cost & Operations (y=21)
      ###################################

      # Lambda duration by function — 30-day time series
      {
        type   = "metric"
        x      = 0
        y      = 21
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.fx_ingest.function_name, { label = "FX Ingest" }],
            ["...", module.ecb_ingest.function_name, { label = "ECB Ingest" }],
            ["...", module.fred_ingest.function_name, { label = "FRED Ingest" }],
            ["...", aws_lambda_function.check_query_results.function_name, { label = "Validation" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Lambda Duration by Function (p50)"
          period  = 86400
          stat    = "p50"
          yAxis   = { left = { label = "ms" } }
        }
      },

      # dbt CodeBuild metrics — time series
      {
        type   = "metric"
        x      = 8
        y      = 21
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/CodeBuild", "Duration", "ProjectName", aws_codebuild_project.dbt_transform.name, { label = "Build Duration", stat = "p90" }],
            ["AWS/CodeBuild", "FailedBuilds", "ProjectName", aws_codebuild_project.dbt_transform.name, { label = "Failed Builds" }],
            ["AWS/CodeBuild", "SucceededBuilds", "ProjectName", aws_codebuild_project.dbt_transform.name, { label = "Succeeded Builds" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "dbt Transform (CodeBuild) Performance"
          period  = 86400
          stat    = "Sum"
        }
      },

      # Operational stats — errors, SNS, unauthorized
      {
        type   = "metric"
        x      = 16
        y      = 21
        width  = 8
        height = 6
        properties = {
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.fx_ingest.function_name, { label = "FX Errors" }],
            ["...", module.ecb_ingest.function_name, { label = "ECB Errors" }],
            ["...", module.fred_ingest.function_name, { label = "FRED Errors" }],
            ["AWS/SNS", "NumberOfNotificationsDelivered", "TopicName", aws_sns_topic.alerts.name, { label = "SNS Delivered" }],
            ["CloudTrailMetrics", "UnauthorizedAPICallCount", { label = "Unauth API Calls" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Errors & Alerts"
          period = 86400
          stat   = "Sum"
        }
      },

      ###################################
      # Row 5 — SLA Compliance (y=28)
      ###################################

      # SLA compliance — 30-day time series with 99.5% target
      {
        type   = "metric"
        x      = 0
        y      = 28
        width  = 12
        height = 6
        properties = {
          metrics = [
            [local.sla_namespace, "PipelineSLACompliance", "Environment", "production", { label = "SLA Compliance", stat = "Average" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Pipeline SLA Compliance (target: 99.5%)"
          period  = 86400
          yAxis   = { left = { min = 0, max = 1, label = "Compliance" } }
          annotations = {
            horizontal = [
              { label = "SLA Target (99.5%)", value = 0.995, color = "#d62728" }
            ]
          }
        }
      },

      # SLA composite alarm status
      {
        type   = "metric"
        x      = 12
        y      = 28
        width  = 12
        height = 6
        properties = {
          metrics = [
            [local.sla_namespace, "PipelineSLACompliance", "Environment", "production", { label = "Current SLA" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Current SLA Status"
          period = 86400
          stat   = "Average"
        }
      },

      ###################################
      # Row 6 — DLQ & Recovery (y=35)
      ###################################

      # DLQ depth — failed executions pending replay
      {
        type   = "metric"
        x      = 0
        y      = 35
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.pipeline_dlq.name, { label = "Failed Executions" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "DLQ Depth (Failed Executions Pending Replay)"
          period  = 300
          stat    = "Maximum"
          yAxis   = { left = { min = 0 } }
          annotations = {
            horizontal = [
              { label = "Alert threshold", value = 1, color = "#ff6961" }
            ]
          }
        }
      },

      ###################################
      # Row 7 — Iceberg Maintenance (y=41)
      ###################################

      {
        type   = "metric"
        x      = 0
        y      = 41
        width  = 12
        height = 6
        properties = {
          metrics = [
            [local.maintenance_namespace, "MaintenanceJobFailed", { label = "Failed Operations", color = "#ff6961" }],
            [local.maintenance_namespace, "MaintenanceOperationsTotal", { label = "Total Operations", color = "#2ca02c" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Iceberg Maintenance (Weekly Compaction & Vacuum)"
          period  = 604800
          stat    = "Sum"
          yAxis   = { left = { min = 0 } }
        }
      },

      ###################################
      # Row 8 — Cross-Source Validation (y=48)
      ###################################

      {
        type   = "metric"
        x      = 0
        y      = 48
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["${var.metric_namespace_prefix}/CrossValidation", "CrossSourceDiscrepancy", { label = "Rate Discrepancies", color = "#d62728" }],
            ["${var.metric_namespace_prefix}/CrossValidation", "RateMismatchCount", { label = "Mismatched Pairs", color = "#ff7f0e" }],
            ["${var.metric_namespace_prefix}/CrossValidation", "TemporalGapDays", { label = "Temporal Gap (days)", color = "#1f77b4" }]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Cross-Source Validation (FX vs ECB)"
          period  = 86400
          stat    = "Maximum"
          yAxis   = { left = { min = 0 } }
          annotations = {
            horizontal = [
              { label = "Rate threshold (1%)", value = 0.01, color = "#d62728" }
            ]
          }
        }
      },

      {
        type   = "metric"
        x      = 12
        y      = 48
        width  = 12
        height = 6
        properties = {
          metrics = [
            ["${var.metric_namespace_prefix}/CrossValidation", "VolumeDeviationPercent", { label = "Max Volume Deviation %" }]
          ]
          view   = "singleValue"
          region = var.aws_region
          title  = "Volume Consistency (Last 24h)"
          period = 86400
          stat   = "Maximum"
        }
      }
    ]
  })
}
