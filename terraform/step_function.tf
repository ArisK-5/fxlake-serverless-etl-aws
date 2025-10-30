resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Invoke Lambda, then Glue, then Athena (sync)",
    StartAt = "InvokeLambda",
    States = {
      InvokeLambda = {
        Type       = "Task",
        Resource   = "arn:aws:states:::lambda:invoke",
        Parameters = { FunctionName = aws_lambda_function.api_ingest.arn },
        Next       = "StartGlueJob"
      },
      StartGlueJob = {
        Type       = "Task",
        Resource   = "arn:aws:states:::glue:startJobRun.sync",
        Parameters = { JobName = aws_glue_job.transform.name },
        Next       = "StartAthenaQuery"
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
}
