# Reusable Lambda function module
#
# Creates a Lambda function with:
#   - Dedicated IAM role (least-privilege)
#   - CloudWatch log group with 14-day retention
#   - X-Ray active tracing
#   - S3 bucket access (optional)
#   - Additional IAM policy (optional)

resource "aws_iam_role" "lambda" {
  name = "${var.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_policy" "s3_access" {
  count = length(var.s3_bucket_arns) > 0 ? 1 : 0

  name = "${var.function_name}-s3"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket"
      ]
      Resource = flatten([
        for arn in var.s3_bucket_arns : [arn, "${arn}/*"]
      ])
    }]
  })
}

resource "aws_iam_role_policy_attachment" "s3_access" {
  count = length(var.s3_bucket_arns) > 0 ? 1 : 0

  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.s3_access[0].arn
}

resource "aws_iam_role_policy" "additional" {
  count = var.additional_policy_json != null ? 1 : 0

  name   = "${var.function_name}-additional"
  role   = aws_iam_role.lambda.id
  policy = var.additional_policy_json
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  description      = var.description
  handler          = var.handler
  runtime          = var.runtime
  role             = aws_iam_role.lambda.arn
  filename         = var.filename
  timeout          = var.timeout
  source_code_hash = filebase64sha256(var.filename)

  tracing_config {
    mode = var.tracing_mode
  }

  environment {
    variables = var.env_vars
  }

  tags = var.tags

  depends_on = [aws_cloudwatch_log_group.lambda]
}
