resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Lambda-Glue-Athena Pipeline",
    StartAt = "Lambda-API-Ingestion",
    States = {
      Lambda-API-Ingestion = {
        Type       = "Task",
        Resource   = "arn:aws:states:::lambda:invoke",
        Parameters = { FunctionName = aws_lambda_function.api_ingest.arn },
        # ResultPath preserves ingestion output at $.ingestion so downstream states
        # can read $.ingestion.Payload.end_date without Glue overwriting it.
        ResultPath     = "$.ingestion",
        TimeoutSeconds = 90,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 3,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Ingestion-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Check-New-Data"
      },
      # Choice states cannot have Retry or Catch blocks — error handling must be in
      # the preceding Task state (Lambda-API-Ingestion). Choice states do not write to
      # the execution state, so $.ingestion.Payload.end_date written by Lambda-API-Ingestion
      # passes through unchanged and is still available when Lambda-Update-State runs.
      Check-New-Data = {
        Type = "Choice",
        Choices = [
          {
            Variable     = "$.ingestion.Payload.status"
            StringEquals = "no_new_data"
            Next         = "Pipeline-Already-Up-To-Date"
          }
        ],
        Default = "Glue-JSON-to-Parquet"
      },
      Pipeline-Already-Up-To-Date = {
        Type = "Succeed"
      },
      Glue-JSON-to-Parquet = {
        Type       = "Task",
        Resource   = "arn:aws:states:::glue:startJobRun.sync",
        Parameters = { JobName = aws_glue_job.transform.name },
        # ResultPath preserves $.ingestion so Lambda-Update-State can read end_date.
        ResultPath     = "$.glue",
        TimeoutSeconds = 180,
        Retry = [
          {
            ErrorEquals     = ["Glue.ConcurrentRunsExceededException", "States.HeartbeatTimeout"],
            IntervalSeconds = 10,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Transform-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Lambda-Update-State"
      },
      # Commit last_processed_date to DynamoDB only after Glue succeeds.
      # This prevents state corruption when Glue fails after ingestion.
      Lambda-Update-State = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = aws_lambda_function.api_ingest.arn,
          Payload = {
            "action"     = "update_state",
            "end_date.$" = "$.ingestion.Payload.end_date"
          }
        },
        ResultPath     = "$.state_update",
        TimeoutSeconds = 30,
        Retry = [
          {
            # States.TaskFailed is a broader fallback for task-layer failures not caught
            # by the specific Lambda codes above. DynamoDB throttles inside the Lambda
            # surface as Lambda.AWSLambdaException; States.TaskFailed covers orchestration-
            # level failures (e.g. resource limits hit at the Step Functions layer).
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "States.TaskFailed"],
            IntervalSeconds = 3,
            MaxAttempts     = 3,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "UpdateState-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Athena-Sample-Query"
      },
      Athena-Sample-Query = {
        Type     = "Task",
        Resource = "arn:aws:states:::athena:startQueryExecution.sync",
        Parameters = {
          QueryString = "SELECT * FROM exchange_rates LIMIT 100;",
          QueryExecutionContext = {
            Database = aws_glue_catalog_database.fxlake.name
          },
          ResultConfiguration = {
            OutputLocation = "s3://${var.athena_results_bucket_name}/results/"
          },
          ResultReuseConfiguration = {
            ResultReuseByAgeConfiguration = {
              Enabled         = true,
              MaxAgeInMinutes = 10
            }
          }
        },
        # ResultPath preserves prior context; Lambda-Validation-Query reads $.athena.
        ResultPath     = "$.athena",
        TimeoutSeconds = 90,
        Retry = [
          {
            ErrorEquals     = ["Athena.InternalServerException", "Athena.TooManyRequestsException"],
            IntervalSeconds = 5,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Query-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Lambda-Validation-Query"
      },
      Lambda-Validation-Query = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = aws_lambda_function.check_query_results.arn,
          Payload = {
            "QueryExecutionId.$" = "$.athena.QueryExecution.QueryExecutionId"
          }
        },
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 3,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Validation-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        End = true
      },
      Ingestion-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Transform-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      UpdateState-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Query-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Validation-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      }
    }
  })

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"
    log_destination        = "${aws_cloudwatch_log_group.stepfunctions_logs.arn}:*"
  }

  depends_on = [
    aws_cloudwatch_log_group.stepfunctions_logs,
    aws_iam_role_policy.sfn_policy
  ]
}
