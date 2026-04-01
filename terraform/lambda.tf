resource "aws_lambda_function" "api_ingest" {
  function_name    = var.lambda_ingestion_name
  description      = "Fetches historical exchange rates from Frankfurter API and stores them in S3 for ETL processing"
  handler          = "lambda_ingestion_function.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda_ingestion_function.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda_ingestion_function.zip")
  environment {
    variables = {
      RAW_BUCKET    = var.raw_bucket_name
      START_DATE    = var.fx_start_date
      END_DATE      = var.fx_end_date
      BASE_CURRENCY = var.fx_base_currency
      BASE_API_URL  = var.fx_base_api_url
      STATE_TABLE   = aws_dynamodb_table.pipeline_state.name
    }
  }
}

resource "aws_lambda_function" "ecb_ingest" {
  function_name    = var.lambda_ecb_ingestion_name
  description      = "Fetches historical exchange rates from ECB Statistics Data Warehouse and stores them in S3 for ETL processing"
  handler          = "lambda_ecb_ingestion.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda_ecb_ingestion.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda_ecb_ingestion.zip")
  environment {
    variables = {
      RAW_BUCKET   = var.raw_bucket_name
      START_DATE   = var.fx_start_date
      END_DATE     = var.fx_end_date
      ECB_BASE_URL = var.ecb_base_url
      STATE_TABLE  = aws_dynamodb_table.pipeline_state.name
    }
  }
}

resource "aws_lambda_function" "check_query_results" {
  function_name    = var.lambda_validation_name
  description      = "Checks Athena query results and publishes custom CloudWatch metric"
  handler          = "lambda_validation_function.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda_validation_function.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda_validation_function.zip")
  environment {
    variables = {
      METRIC_NAMESPACE = "${var.metric_namespace_prefix}/Athena"
      PIPELINE         = var.pipeline
    }
  }
}

# EventBridge (CloudWatch Events) scheduled rule — triggers the full pipeline daily
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "fxlake-daily-ingest"
  description         = "Daily trigger for FXLake ETL Step Functions pipeline"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "invoke_step_function" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "stepfunctions-ETL-Pipeline"
  arn       = aws_sfn_state_machine.etl.arn
  role_arn  = aws_iam_role.eventbridge_sfn_invoke_role.arn
}
