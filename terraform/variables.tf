variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "raw_bucket_name" {
  description = "S3 bucket for raw API JSON"
  type        = string
  default     = "fxlake-raw-data-2025-unique" # change to unique name
}

variable "processed_bucket_name" {
  description = "S3 bucket for processed CSV/Parquet"
  type        = string
  default     = "fxlake-processed-data-2025-unique" # change to unique name
}

variable "lambda_function_name" {
  type    = string
  default = "fxlake-api-ingest-lambda"
}

variable "glue_job_name" {
  type    = string
  default = "fxlake-glue-transform-job"
}

variable "glue_script_s3_key" {
  description = "S3 key for Glue job script. If empty, update Glue job to use local script upload workflow."
  type        = string
  default     = "glue/glue_transform.py"
}

variable "athena_results_bucket" {
  type    = string
  default = "fxlake-athena-query-results-2025-unique" # change to unique name
}

variable "fx_start_date" {
  description = "Start date for FX rate data collection (YYYY-MM-DD)"
  type        = string
  default     = "2024-01-01"
}

variable "fx_end_date" {
  description = "End date for FX rate data collection (YYYY-MM-DD)"
  type        = string
  default     = "2024-12-31"
}

variable "fx_base_currency" {
  description = "Base currency for exchange rates"
  type        = string
  default     = "EUR"
}

variable "fx_output_format" {
  description = "Output format for processed exchange rate data (csv or parquet)"
  type        = string
  default     = "parquet"
  validation {
    condition     = contains(["csv", "parquet"], var.fx_output_format)
    error_message = "fx_output_format must be either 'csv' or 'parquet'"
  }
}
