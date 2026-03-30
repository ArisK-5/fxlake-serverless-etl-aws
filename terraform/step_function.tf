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
            Next        = "Ingestion-Failed"
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
            ErrorEquals     = ["States.TaskFailed"],
            IntervalSeconds = 10,
            MaxAttempts     = 1,
            BackoffRate     = 1.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Transform-Failed"
          }
        ],
        Next = "Athena-Sample-Query"
      },
      Athena-Sample-Query = {
        Type     = "Task",
        Resource = "arn:aws:states:::athena:startQueryExecution.sync",
        Parameters = {
          QueryString = "SELECT * FROM exchange_rates LIMIT 100;", # sample query
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
            Next        = "Query-Failed"
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
            Next        = "Validation-Failed"
          }
        ],
        End = true
      },
      Ingestion-Failed = {
        Type  = "Fail",
        Error = "IngestionError",
        Cause = "Lambda ingestion failed: unable to fetch exchange rates from API or write to S3 raw bucket."
      },
      Transform-Failed = {
        Type  = "Fail",
        Error = "TransformError",
        Cause = "Glue transform job failed: unable to process raw JSON and write Parquet to the processed S3 bucket."
      },
      Query-Failed = {
        Type  = "Fail",
        Error = "AthenaQueryError",
        Cause = "Athena query execution failed: unable to run sample query against the processed exchange rates table."
      },
      Validation-Failed = {
        Type  = "Fail",
        Error = "ValidationError",
        Cause = "Validation Lambda failed: unable to verify Athena query results or publish CloudWatch metric."
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
