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
