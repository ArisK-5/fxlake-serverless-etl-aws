resource "aws_lambda_function" "api_ingest" {
  function_name    = var.lambda_function_name
  description      = "Fetches historical exchange rates from Frankfurter API and stores them in S3 for ETL processing"
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.10"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda.zip")
  environment {
    variables = {
      RAW_BUCKET    = var.raw_bucket_name
      START_DATE    = var.fx_start_date
      END_DATE      = var.fx_end_date
      BASE_CURRENCY = var.fx_base_currency
    }
  }
}

# EventBridge (CloudWatch Events) scheduled rule — daily by default
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "fxlake-daily-ingest"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "invoke_lambda" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "invokeLambda"
  arn       = aws_lambda_function.api_ingest.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_ingest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}
