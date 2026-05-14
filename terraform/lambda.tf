resource "aws_lambda_function" "fx_ingest" {
  function_name    = var.lambda_fx_ingestion_name
  description      = "Fetches historical exchange rates from Frankfurter API and stores them in S3 for ETL processing"
  handler          = "lambda_fx_ingestion.lambda_handler"
  runtime          = "python3.12"
  role             = aws_iam_role.lambda_exec.arn
  filename         = "../lambda/lambda_fx_ingestion.zip"
  timeout          = 60
  source_code_hash = filebase64sha256("../lambda/lambda_fx_ingestion.zip")
  tracing_config {
    mode = "Active"
  }

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

  tags = {
    component = "ingestion"
    source    = "frankfurter"
  }
}

# ECB and FRED Lambdas use the reusable module (dedicated IAM role per function)

locals {
  dynamodb_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:GetItem",
        "dynamodb:PutItem"
      ]
      Resource = aws_dynamodb_table.pipeline_state.arn
    }]
  })
}

module "ecb_ingest" {
  source = "./modules/lambda_function"

  function_name = var.lambda_ecb_ingestion_name
  description   = "Fetches historical exchange rates from ECB Statistics Data Warehouse and stores them in S3 for ETL processing"
  handler       = "lambda_ecb_ingestion.lambda_handler"
  filename      = "../lambda/lambda_ecb_ingestion.zip"

  env_vars = {
    RAW_BUCKET   = var.raw_bucket_name
    START_DATE   = var.fx_start_date
    END_DATE     = var.fx_end_date
    ECB_BASE_URL = var.ecb_base_url
    STATE_TABLE  = aws_dynamodb_table.pipeline_state.name
  }

  s3_bucket_arns         = [aws_s3_bucket.raw.arn]
  additional_policy_json = local.dynamodb_policy_json

  tags = {
    component = "ingestion"
    source    = "ecb"
  }
}

module "fred_ingest" {
  source = "./modules/lambda_function"

  function_name = var.lambda_fred_ingestion_name
  description   = "Fetches economic indicator observations from the FRED API and stores them in S3 for ETL processing"
  handler       = "lambda_fred_ingestion.lambda_handler"
  filename      = "../lambda/lambda_fred_ingestion.zip"

  env_vars = {
    RAW_BUCKET    = var.raw_bucket_name
    START_DATE    = var.fx_start_date
    END_DATE      = var.fx_end_date
    FRED_BASE_URL = var.fred_base_url
    FRED_SERIES   = var.fred_series
    FRED_API_KEY  = var.fred_api_key
    STATE_TABLE   = aws_dynamodb_table.pipeline_state.name
  }

  s3_bucket_arns         = [aws_s3_bucket.raw.arn]
  additional_policy_json = local.dynamodb_policy_json

  tags = {
    component = "ingestion"
    source    = "fred"
  }
}

module "iceberg_writer" {
  source = "./modules/lambda_function"

  function_name = var.lambda_iceberg_writer_name
  description   = "Writes transformed data to Iceberg tables via Athena INSERT INTO with quality gates"
  handler       = "lambda_iceberg_writer.lambda_handler"
  filename      = "../lambda/lambda_iceberg_writer.zip"
  timeout       = 300

  env_vars = {
    DATABASE_NAME         = aws_glue_catalog_database.fxlake.name
    ATHENA_RESULTS_BUCKET = aws_s3_bucket.athena_results.bucket
    ATHENA_WORKGROUP      = aws_athena_workgroup.fxlake.name
    RAW_BUCKET            = aws_s3_bucket.raw.bucket
    PROCESSED_BUCKET      = aws_s3_bucket.processed.bucket
    QUARANTINE_BUCKET     = aws_s3_bucket.quarantine.bucket
    METRIC_NAMESPACE      = "${var.metric_namespace_prefix}/Quality"
  }

  s3_bucket_arns = [
    aws_s3_bucket.raw.arn,
    aws_s3_bucket.athena_results.arn,
    aws_s3_bucket.processed.arn,
    aws_s3_bucket.quarantine.arn,
  ]

  additional_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution"
        ]
        Resource = aws_athena_workgroup.fxlake.arn
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetPartitions",
          "glue:UpdateTable"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.fxlake.name}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.fxlake.name}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "${var.metric_namespace_prefix}/Quality"
          }
        }
      }
    ]
  })

  tags = {
    component = "transform"
    source    = "iceberg"
  }
}

module "data_validator" {
  source = "./modules/lambda_function"

  function_name = var.lambda_data_validator_name
  description   = "Validates Iceberg table data integrity — row counts, null checks, expected values"
  handler       = "lambda_data_validator.lambda_handler"
  filename      = "../lambda/lambda_data_validator.zip"
  timeout       = 300

  env_vars = {
    DATABASE_NAME         = aws_glue_catalog_database.fxlake.name
    ATHENA_RESULTS_BUCKET = aws_s3_bucket.athena_results.bucket
    ATHENA_WORKGROUP      = aws_athena_workgroup.fxlake.name
    METRIC_NAMESPACE      = "${var.metric_namespace_prefix}/Validation"
  }

  s3_bucket_arns = [
    aws_s3_bucket.athena_results.arn,
    aws_s3_bucket.processed.arn,
  ]

  additional_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults"
        ]
        Resource = aws_athena_workgroup.fxlake.arn
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.fxlake.name}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.fxlake.name}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "${var.metric_namespace_prefix}/Validation"
          }
        }
      }
    ]
  })

  tags = {
    component = "validation"
    source    = "iceberg"
  }
}

module "cross_validator" {
  source = "./modules/lambda_function"

  function_name = var.lambda_cross_validator_name
  description   = "Cross-source validation — compares FX rates across Frankfurter and ECB for consistency"
  handler       = "lambda_cross_validator.lambda_handler"
  filename      = "../lambda/lambda_cross_validator.zip"
  timeout       = 300

  env_vars = {
    DATABASE_NAME         = aws_glue_catalog_database.fxlake.name
    ATHENA_RESULTS_BUCKET = aws_s3_bucket.athena_results.bucket
    ATHENA_WORKGROUP      = aws_athena_workgroup.fxlake.name
    METRIC_NAMESPACE      = "${var.metric_namespace_prefix}/CrossValidation"
  }

  s3_bucket_arns = [
    aws_s3_bucket.athena_results.arn,
    aws_s3_bucket.processed.arn,
  ]

  additional_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults"
        ]
        Resource = aws_athena_workgroup.fxlake.arn
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.fxlake.name}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.fxlake.name}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "${var.metric_namespace_prefix}/CrossValidation"
          }
        }
      }
    ]
  })

  tags = {
    component = "validation"
    source    = "cross-source"
  }
}

module "anomaly_detector" {
  source = "./modules/lambda_function"

  function_name = var.lambda_anomaly_detector_name
  description   = "Statistical anomaly detection for FX rates and economic indicators using z-score analysis"
  handler       = "lambda_anomaly_detector.lambda_handler"
  filename      = "../lambda/lambda_anomaly_detector.zip"
  timeout       = 300

  env_vars = {
    DATABASE_NAME         = aws_glue_catalog_database.fxlake.name
    ATHENA_RESULTS_BUCKET = aws_s3_bucket.athena_results.bucket
    ATHENA_WORKGROUP      = aws_athena_workgroup.fxlake.name
    METRIC_NAMESPACE      = "${var.metric_namespace_prefix}/AnomalyDetection"
    SNS_TOPIC_ARN         = aws_sns_topic.alerts.arn
  }

  s3_bucket_arns = [
    aws_s3_bucket.athena_results.arn,
    aws_s3_bucket.processed.arn,
  ]

  additional_policy_json = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults"
        ]
        Resource = aws_athena_workgroup.fxlake.arn
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${aws_glue_catalog_database.fxlake.name}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${aws_glue_catalog_database.fxlake.name}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "${var.metric_namespace_prefix}/AnomalyDetection"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })

  tags = {
    component = "validation"
    source    = "anomaly-detection"
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
  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      METRIC_NAMESPACE = "${var.metric_namespace_prefix}/Athena"
      PIPELINE         = var.pipeline
      SLA_NAMESPACE    = "${var.metric_namespace_prefix}/SLA"
    }
  }

  tags = {
    component = "validation"
  }
}

# EventBridge (CloudWatch Events) scheduled rule — triggers the full pipeline daily
resource "aws_cloudwatch_event_rule" "daily" {
  name                = "fxlake-daily-ingest"
  description         = "Daily trigger for FXLake ETL Step Functions pipeline"
  schedule_expression = "rate(1 day)"

  tags = {
    component = "orchestration"
  }
}

resource "aws_cloudwatch_event_target" "invoke_step_function" {
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "stepfunctions-ETL-Pipeline"
  arn       = aws_sfn_state_machine.etl.arn
  role_arn  = aws_iam_role.eventbridge_sfn_invoke_role.arn
}
