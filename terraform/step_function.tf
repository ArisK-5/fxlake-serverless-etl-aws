resource "aws_sfn_state_machine" "etl" {
  name     = "fxlake-etl-state-machine"
  role_arn = aws_iam_role.sfn_role.arn
  definition = jsonencode({
    Comment = "FXLake ETL: Parallel Lambda-Glue-Athena Pipeline",
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
      # Skip to Succeed only when ALL THREE sources are already up to date.
      # If any source has new data the pipeline runs Glue → Update-State → Athena.
      # Choice states cannot have Retry/Catch — error handling is in Parallel-Ingestion above.
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
        Default = "Glue-JSON-to-Parquet"
      },
      Pipeline-Already-Up-To-Date = {
        Type = "Succeed"
      },
      Glue-JSON-to-Parquet = {
        Type       = "Task",
        Resource   = "arn:aws:states:::glue:startJobRun.sync",
        Parameters = { JobName = aws_glue_job.transform.name },
        # ResultPath preserves $.parallel_results so Lambda-Update-*-State can read end_date.
        ResultPath     = "$.glue",
        TimeoutSeconds = 600,
        Retry = [
          {
            ErrorEquals     = ["Glue.ConcurrentRunsExceededException", "States.HeartbeatTimeout"],
            IntervalSeconds = 10,
            MaxAttempts     = 2,
            BackoffRate     = 2.0
          }
        ],
        Catch = [
          {
            ErrorEquals = ["States.ALL"],
            Next        = "Transform-Failed",
            ResultPath  = "$.errorInfo"
          }
        ],
        Next = "Write-FX-Iceberg"
      },
      Write-FX-Iceberg = {
        Type     = "Task",
        Resource = "arn:aws:states:::lambda:invoke",
        Parameters = {
          FunctionName = module.iceberg_writer.function_arn,
          Payload = {
            "raw_bucket.$"  = "$.parallel_results.fx.Payload.bucket",
            "raw_key.$"     = "$.parallel_results.fx.Payload.raw_key",
            "target_table"  = "fx_rates",
            "database_name" = aws_glue_catalog_database.fxlake.name
          }
        },
        ResultPath     = "$.iceberg_write",
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
        Next = "Check-Backfill-Mode"
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
      # Commit Frankfurter last_processed_date to DynamoDB only after Glue succeeds.
      # Prevents state corruption if Glue fails after ingestion writes the raw file.
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
      # Commit ECB last_processed_date to DynamoDB after FX state is committed.
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
      # Commit FRED last_processed_date to DynamoDB after ECB state is committed.
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
      # Data freshness query: verifies Glue wrote Parquet and checks how recent the data is.
      # Queries fx_rates only — FX rates are the core product; economic_indicators validated
      # implicitly by Glue success. Validation Lambda parses latest_date + total_records.
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
        End = true
      },
      Ingestion-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Transform-Failed = {
        Type      = "Fail",
        ErrorPath = "$.errorInfo.Error",
        CausePath = "$.errorInfo.Cause"
      },
      Write-FX-Iceberg-Failed = {
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
      }
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
