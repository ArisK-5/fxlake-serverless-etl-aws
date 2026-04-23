resource "aws_glue_catalog_database" "fxlake" {
  name        = "fxlake"
  description = "Database for FXLake multi-domain ETL data"
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
