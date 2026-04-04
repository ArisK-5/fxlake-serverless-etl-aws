resource "aws_glue_catalog_database" "fxlake" {
  name        = "fxlake"
  description = "Database for FXLake multi-domain ETL data"
}

resource "aws_glue_catalog_table" "fx_rates" {
  name          = "fx_rates"
  database_name = aws_glue_catalog_database.fxlake.name

  table_type = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL              = "TRUE"
    "parquet.compression" = "SNAPPY"

    # Partition projection — eliminates manual MSCK REPAIR TABLE
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2020,2030"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.processed.bucket}/fx_rates/year=$${year}/month=$${month}/day=$${day}"
  }

  partition_keys {
    name = "year"
    type = "int"
  }

  partition_keys {
    name = "month"
    type = "int"
  }

  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.processed.bucket}/fx_rates/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "ParquetHiveSerDe"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
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

resource "aws_glue_catalog_table" "economic_indicators" {
  name          = "economic_indicators"
  database_name = aws_glue_catalog_database.fxlake.name

  table_type = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL              = "TRUE"
    "parquet.compression" = "SNAPPY"

    # Partition projection — eliminates manual MSCK REPAIR TABLE
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2020,2030"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.processed.bucket}/economic_indicators/year=$${year}/month=$${month}/day=$${day}"
  }

  partition_keys {
    name = "year"
    type = "int"
  }

  partition_keys {
    name = "month"
    type = "int"
  }

  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.processed.bucket}/economic_indicators/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "ParquetHiveSerDe"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
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
