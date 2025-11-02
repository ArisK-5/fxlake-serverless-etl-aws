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
    }
  }
}

resource "aws_lambda_function" "check_query_results" {
  function_name    = var.lambda_check_name
  description      = "Checks Athena query results and publishes custom CloudWatch metric"
  handler          = "lambda_validation_function.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda_validation_function.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda_validation_function.zip")

  environment {
    variables = {
      LOG_LEVEL = "INFO"
    }
  }
}

# EventBridge (CloudWatch Events) scheduled rule — daily by default
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "fxlake-daily-ingest"
  description         = "Daily trigger for FXLake Lambda Ingestion"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "invoke_lambda" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "lambda-API-Ingestion"
  arn       = aws_lambda_function.api_ingest.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_ingest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}
