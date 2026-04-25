# Iceberg table definitions for FXLake
# Uses AWS provider v5 syntax (open_table_format_input + storage_descriptor)

resource "aws_glue_catalog_table" "fx_rates_iceberg" {
  name          = "fx_rates"
  database_name = aws_glue_catalog_database.fxlake.name
  table_type    = "EXTERNAL_TABLE"

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location = "s3://${aws_s3_bucket.processed.bucket}/iceberg/fx_rates/"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
    columns {
      name = "base_currency"
      type = "string"
    }
    columns {
      name = "target_currency"
      type = "string"
    }
    columns {
      name = "rate"
      type = "double"
    }
  }
}

resource "aws_glue_catalog_table" "economic_indicators_iceberg" {
  name          = "economic_indicators"
  database_name = aws_glue_catalog_database.fxlake.name
  table_type    = "EXTERNAL_TABLE"

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  storage_descriptor {
    location = "s3://${aws_s3_bucket.processed.bucket}/iceberg/economic_indicators/"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
    }

    columns {
      name = "date"
      type = "string"
    }
    columns {
      name = "source"
      type = "string"
    }
    columns {
      name = "series_id"
      type = "string"
    }
    columns {
      name = "value"
      type = "double"
    }
  }
}

# -----------------------------------------------------------
# Iceberg Maintenance Lambda
# -----------------------------------------------------------

module "iceberg_maintenance" {
  source = "./modules/lambda_function"

  function_name = var.lambda_iceberg_maintenance_name
  description   = "Runs OPTIMIZE and VACUUM on Iceberg tables for compaction and snapshot expiry"
  handler       = "lambda_iceberg_maintenance.lambda_handler"
  filename      = "../lambda/lambda_iceberg_maintenance.zip"
  timeout       = 900

  env_vars = {
    DATABASE_NAME         = aws_glue_catalog_database.fxlake.name
    ATHENA_RESULTS_BUCKET = aws_s3_bucket.athena_results.bucket
    ATHENA_WORKGROUP      = aws_athena_workgroup.fxlake.name
    METRIC_NAMESPACE      = "${var.metric_namespace_prefix}/Maintenance"
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
            "cloudwatch:namespace" = "${var.metric_namespace_prefix}/Maintenance"
          }
        }
      }
    ]
  })

  tags = {
    component = "maintenance"
    source    = "iceberg"
  }
}

# -----------------------------------------------------------
# EventBridge — Weekly Iceberg Maintenance (Sunday 6 AM UTC)
# -----------------------------------------------------------

resource "aws_cloudwatch_event_rule" "iceberg_maintenance" {
  name                = "fxlake-iceberg-maintenance"
  description         = "Weekly Iceberg table compaction and snapshot expiry"
  schedule_expression = "cron(0 6 ? * SUN *)"

  tags = {
    component = "maintenance"
  }
}

resource "aws_cloudwatch_event_target" "iceberg_maintenance" {
  rule      = aws_cloudwatch_event_rule.iceberg_maintenance.name
  target_id = "iceberg-maintenance-lambda"
  arn       = module.iceberg_maintenance.function_arn
}

resource "aws_lambda_permission" "eventbridge_iceberg_maintenance" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.iceberg_maintenance.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.iceberg_maintenance.arn
}
