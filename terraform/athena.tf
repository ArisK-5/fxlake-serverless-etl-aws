resource "aws_glue_catalog_database" "fxlake" {
  name        = "fxlake"
  description = "FXLake data lake — daily foreign exchange rates (Frankfurter, ECB) and economic indicators (FRED), stored as Apache Iceberg tables with ACID transactions and time travel."

  parameters = {
    "owner"               = "fxlake-pipeline"
    "classification"      = "financial-data"
    "data_classification" = "internal"
  }
}

resource "aws_athena_workgroup" "fxlake" {
  name = "fxlake"

  tags = {
    component = "query"
  }

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    engine_version {
      selected_engine_version = "AUTO"
    }

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/results/"
    }
  }
}
