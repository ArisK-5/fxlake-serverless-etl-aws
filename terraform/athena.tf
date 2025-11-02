resource "aws_glue_catalog_database" "fxlake" {
  name        = "fxlake"
  description = "Database for FX exchange rates data"
}

resource "aws_glue_catalog_table" "exchange_rates" {
  name          = "exchange_rates"
  database_name = aws_glue_catalog_database.fxlake.name

  table_type = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL              = "TRUE"
    "parquet.compression" = "SNAPPY"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.processed.bucket}/exchange_rates/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "ParquetHiveSerDe"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
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
    columns {
      name = "date"
      type = "string"
    }
  }
}

resource "aws_athena_workgroup" "fxlake" {
  name = "fxlake"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/results/"
    }
  }
}
