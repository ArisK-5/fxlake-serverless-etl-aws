# Dead Letter Queue (DLQ) for failed Step Functions executions
# EventBridge captures FAILED/TIMED_OUT executions and routes them to SQS
# Enables manual replay of failed pipelines via make replay-dlq

# SQS queue for failed executions
resource "aws_sqs_queue" "pipeline_dlq" {
  name                       = "fxlake-pipeline-dlq"
  message_retention_seconds  = 1209600 # 14 days
  visibility_timeout_seconds = 300
  sqs_managed_sse_enabled    = true

  tags = {
    component = "monitoring"
    layer     = "dlq"
  }
}

# Queue policy — allow EventBridge to send messages
resource "aws_sqs_queue_policy" "pipeline_dlq" {
  queue_url = aws_sqs_queue.pipeline_dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.pipeline_dlq.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.sfn_failure.arn
          }
        }
      }
    ]
  })
}

# EventBridge rule — capture FAILED and TIMED_OUT executions
resource "aws_cloudwatch_event_rule" "sfn_failure" {
  name        = "fxlake-sfn-execution-failure"
  description = "Capture failed and timed-out Step Functions executions"

  event_pattern = jsonencode({
    source      = ["aws.states"]
    detail-type = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["FAILED", "TIMED_OUT"]
      stateMachineArn = [aws_sfn_state_machine.etl.arn]
    }
  })

  tags = {
    component = "monitoring"
  }
}

# EventBridge target — route to SQS DLQ
resource "aws_cloudwatch_event_target" "dlq" {
  rule      = aws_cloudwatch_event_rule.sfn_failure.name
  target_id = "pipeline-dlq"
  arn       = aws_sqs_queue.pipeline_dlq.arn
}

# CloudWatch alarm — trigger when messages accumulate in DLQ
resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name          = "fxlake-dlq-messages"
  alarm_description   = "One or more pipeline executions have failed and are waiting for replay"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.pipeline_dlq.name
  }

  tags = {
    component = "monitoring"
  }
}
