resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Parallel Ingestion → Iceberg Write → dbt Transform → Validation",
    StartAt = "Parallel-Ingestion",
    States = {
      # Runs Frankfurter, ECB, and FRED ingestion concurrently.
      # Output array [$[0], $[1], $[2]] is reshaped by ResultSelector to named keys.
      # ResultPath merges into input so downstream states still see the full execution context.
      Parallel-Ingestion = {
        Type = "Parallel",
        Branches = [
          {
            StartAt = "Lambda-FX-Ingestion",
            States = {
              Lambda-FX-Ingestion = {
                Type     = "Task",
                Resource = "arn:aws:states:::lambda:invoke",
                Parameters = {
                  FunctionName = aws_lambda_function.fx_ingest.arn,
                  "Payload.$"  = "$"
                },
                TimeoutSeconds = 90,
                Retry = [
                  {
                    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
                    IntervalSeconds = 3,
                    MaxAttempts     = 2,
                    BackoffRate     = 2.0
                  }
                ],
                End = true
              }
            }
          },
          {
            StartAt = "Lambda-ECB-Ingestion",
            States = {
              Lambda-ECB-Ingestion = {
                Type     = "Task",
                Resource = "arn:aws:states:::lambda:invoke",
                Parameters = {
                  FunctionName = module.ecb_ingest.function_arn,
                  "Payload.$"  = "$"
                },
                TimeoutSeconds = 90,
                Retry = [
                  {
                    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
                    IntervalSeconds = 3,
                    MaxAttempts     = 2,
                    BackoffRate     = 2.0
                  }
                ],
                End = true
              }
            }
          },
          {
            StartAt = "Lambda-FRED-Ingestion",
            States = {
              Lambda-FRED-Ingestion = {
                Type     = "Task",
                Resource = "arn:aws:states:::lambda:invoke",
                Parameters = {
                  FunctionName = module.fred_ingest.function_arn,
                  "Payload.$"  = "$"
                },
                TimeoutSeconds = 90,
                Retry = [
                  {
                    ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
                    IntervalSeconds = 3,
                    MaxAttempts     = 2,
                    BackoffRate     = 2.0
                  }
                ],
                End = true
              }
            }
          }
        ],
        ResultSelector = {
          "fx.$"   = "$[0]",
          "ecb.$"  = "$[1]",
          "fred.$" = "$[2]"
        },
        ResultPath = "$.parallel_results",
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Ingestion-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Check-New-Data"
      },
      Check-New-Data = {
        Type = "Choice",
        Choices = [
          {
            And = [
              {
                Variable     = "$.parallel_results.fx.Payload.status",
                StringEquals = "no_new_data"
              },
              {
                Variable     = "$.parallel_results.ecb.Payload.status",
                StringEquals = "no_new_data"
              },
              {
                Variable     = "$.parallel_results.fred.Payload.status",
                StringEquals = "no_new_data"
              }
            ],
            Next = "Pipeline-Already-Up-To-Date"
          }
        ],
        Default = "Write-FX-Iceberg"
      },
      Pipeline-Already-Up-To-Date = {
        Type = "Succeed"
      },
      Write-FX-Iceberg = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.iceberg_writer.function_arn,
          Payload = {
            "raw_bucket"    = aws_s3_bucket.raw.bucket,
            "raw_key.$"     = "$.parallel_results.fx.Payload.key",
            "target_table"  = "fx_rates",
            "domain"        = "fx_rates",
            "database_name" = aws_glue_catalog_database.fxlake.name
          }
        },
        ResultPath     = "$.iceberg_fx_write",
        TimeoutSeconds = 330,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 5,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Write-FX-Iceberg-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Write-Economic-Iceberg"
      },
      Write-Economic-Iceberg = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.iceberg_writer.function_arn,
          Payload = {
            "raw_bucket"    = aws_s3_bucket.raw.bucket,
            "raw_key.$"     = "$.parallel_results.fred.Payload.key",
            "target_table"  = "economic_indicators",
            "domain"        = "economic_indicators",
            "database_name" = aws_glue_catalog_database.fxlake.name
          }
        },
        ResultPath     = "$.iceberg_econ_write",
        TimeoutSeconds = 330,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 5,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Write-Economic-Iceberg-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "dbt-Transform"
      },
      dbt-Transform = {
        Type     = "Task",
        Resource = "arn:aws:states:::codebuild:startBuild.sync",
        Parameters = {
          ProjectName = aws_codebuild_project.dbt_transform.name
        },
        ResultPath     = "$.dbt",
        TimeoutSeconds = 600,
        Retry = [
          {
            ErrorEquals     = ["States.TaskFailed"],
            IntervalSeconds = 10,
            MaxAttempts     = 1,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "dbt-Transform-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Check-Backfill-Mode"
      },
      dbt-Transform-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      # Backfill runs must NOT update DynamoDB state — skip straight to Athena.
      # The "mode" key is only present in backfill payloads (see _perform_ingest).
      Check-Backfill-Mode = {
        Type    = "Choice",
        Comment = "Skip state updates for backfill executions to protect the incremental watermark.",
        Choices = [
          {
            Variable     = "$.parallel_results.fx.Payload.mode",
            StringEquals = "backfill",
            Next         = "Athena-Sample-Query"
          }
        ],
        Default = "Lambda-Update-FX-State"
      },
      Lambda-Update-FX-State = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = aws_lambda_function.fx_ingest.arn,
          Payload = {
            "action"     = "update_state",
            "end_date.$" = "$.parallel_results.fx.Payload.end_date"
          }
        },
        ResultPath     = "$.fx_state_update",
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "States.TaskFailed"],
            IntervalSeconds = 3,
            MaxAttempts     = 3,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "UpdateFXState-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Lambda-Update-ECB-State"
      },
      Lambda-Update-ECB-State = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.ecb_ingest.function_arn,
          Payload = {
            "action"     = "update_state",
            "end_date.$" = "$.parallel_results.ecb.Payload.end_date"
          }
        },
        ResultPath     = "$.ecb_state_update",
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "States.TaskFailed"],
            IntervalSeconds = 3,
            MaxAttempts     = 3,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "UpdateECBState-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Lambda-Update-FRED-State"
      },
      Lambda-Update-FRED-State = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.fred_ingest.function_arn,
          Payload = {
            "action"     = "update_state",
            "end_date.$" = "$.parallel_results.fred.Payload.end_date"
          }
        },
        ResultPath     = "$.fred_state_update",
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException", "States.TaskFailed"],
            IntervalSeconds = 3,
            MaxAttempts     = 3,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "UpdateFREDState-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Athena-Sample-Query"
      },
      Athena-Sample-Query = {
        Type     = "Task",
        Resource = "arn:aws:states:::athena:startQueryExecution.sync",
        Parameters = {
          QueryString = "SELECT MAX(date) AS latest_date, COUNT(*) AS total_records FROM fx_rates;",
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
        # ResultPath preserves prior context; Lambda-Validation-Query reads $.athena.
        ResultPath     = "$.athena",
        TimeoutSeconds = 90,
        Retry = [
          {
            ErrorEquals     = ["Athena.InternalServerException", "Athena.TooManyRequestsException"],
            IntervalSeconds = 5,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Query-Failed",
            ResultPath  = "$.errorInfo"
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
            "QueryExecutionId.$" = "$.athena.QueryExecution.QueryExecutionId"
          }
        },
        TimeoutSeconds = 30,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 3,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Validation-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Cross-Source-Validation"
      },
      Cross-Source-Validation = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.cross_validator.function_arn,
          Payload = {
            "database_name" = aws_glue_catalog_database.fxlake.name
          }
        },
        ResultPath     = "$.cross_validation",
        TimeoutSeconds = 300,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 3,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Cross-Validation-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Anomaly-Detection"
      },
      Anomaly-Detection = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.anomaly_detector.function_arn,
          Payload = {
            "database_name" = aws_glue_catalog_database.fxlake.name
          }
        },
        ResultPath     = "$.anomaly_detection",
        TimeoutSeconds = 300,
        Retry = [
          {
            ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"],
            IntervalSeconds = 3,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Anomaly-Detection-Error-Handled",
            ResultPath  = "$.errorInfo"
          }
        ],
        End = true
      },
      Anomaly-Detection-Error-Handled = {
        Type       = "Pass",
        Result     = "Anomaly detection failed but pipeline continues",
        ResultPath = "$.anomaly_detection_note",
        End        = true
      },
      Ingestion-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Write-FX-Iceberg-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Write-Economic-Iceberg-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      UpdateFXState-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      UpdateECBState-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      UpdateFREDState-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Query-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Validation-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Cross-Validation-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
    }
  })

  tags = {
    component = "orchestration"
  }

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
