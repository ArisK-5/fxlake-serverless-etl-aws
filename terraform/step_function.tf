resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Lambda-Glue-Athena Pipeline",
    StartAt = "Lambda-API-Ingestion",
    States = {
      Lambda-API-Ingestion = {
        Type           = "Task",
        Resource       = "arn:aws:states:::lambda:invoke",
        Parameters     = { FunctionName = aws_lambda_function.api_ingest.arn },
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"],
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
        Next = "Glue-JSON-to-Parquet"
      },
      Glue-JSON-to-Parquet = {
        Type           = "Task",
        Resource       = "arn:aws:states:::glue:startJobRun.sync",
        Parameters     = { JobName = aws_glue_job.transform.name },
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
        TimeoutSeconds = 90,
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"],
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
            "QueryExecutionId.$" = "$.QueryExecution.QueryExecutionId"
          }
        },
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.TooManyRequestsException"],
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
