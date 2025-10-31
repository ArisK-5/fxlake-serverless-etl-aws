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
        TimeoutSeconds = 300,
        Next           = "StartGlueJob"
      },
      StartGlueJob = {
        Type           = "Task",
        Resource       = "arn:aws:states:::glue:startJobRun.sync",
        Parameters     = { JobName = aws_glue_job.transform.name },
        TimeoutSeconds = 300,
        Next           = "StartAthenaQuery"
      },
      StartAthenaQuery = {
        Type     = "Task",
        Resource = "arn:aws:states:::athena:startQueryExecution.sync",
        Parameters = {
          QueryString = "SELECT * FROM exchange_rates LIMIT 100;",
          QueryExecutionContext = {
            Database = aws_glue_catalog_database.fxlake.name
          },
          ResultConfiguration = {
            OutputLocation = "s3://${var.athena_results_bucket}/"
          }
        },
        End = true
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
