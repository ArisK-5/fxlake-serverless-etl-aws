resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Lambda-Glue-Athena Pipeline",
    StartAt = "InvokeLambda",
    States = {
      InvokeLambda = {
        Type           = "Task",
        Resource       = "arn:aws:states:::lambda:invoke",
        Parameters     = { FunctionName = aws_lambda_function.api_ingest.arn },
        TimeoutSeconds = 30,
        Next           = "StartGlueJob"
      },
      StartGlueJob = {
        Type           = "Task",
        Resource       = "arn:aws:states:::glue:startJobRun.sync",
        Parameters     = { JobName = aws_glue_job.transform.name },
        TimeoutSeconds = 90,
        Next           = "StartAthenaQuery"
      },
      StartAthenaQuery = {
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
        Next           = "CheckQueryResults"
      },
      CheckQueryResults = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = aws_lambda_function.check_query_results.arn,
          Payload = {
            "QueryExecutionId.$" = "$.QueryExecution.QueryExecutionId"
          }
        },
        TimeoutSeconds = 30,
        End            = true
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
    aws_iam_role_policy.sfn_policy,
    aws_athena_named_query.fxlake_sample_query
  ]
}
