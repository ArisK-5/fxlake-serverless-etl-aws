variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "raw_bucket_name" {
  description = "S3 bucket for raw API JSON"
  type        = string
}

variable "processed_bucket_name" {
  description = "S3 bucket for processed CSV/Parquet"
  type        = string
}

variable "athena_results_bucket_name" {
  type = string
}

variable "cloudtrail_logs_bucket_name" {
  description = "S3 bucket for CloudTrail logs"
  type        = string
}

variable "lambda_ingestion_name" {
  type    = string
  default = "fxlake-api-ingest-lambda"
}

variable "lambda_check_name" {
  type    = string
  default = "fxlake-results-check-lambda"
}

variable "glue_job_name" {
  type    = string
  default = "fxlake-glue-transform-job"
}

variable "sns_email_address" {
  description = "Email address for SNS notifications"
  type        = string
}

variable "glue_script_s3_key" {
  description = "S3 key for Glue job script"
  type        = string
  default     = "glue/glue_transform.py"
}

variable "fx_base_api_url" {
  description = "Base API URL for exchange rates"
  type        = string
  default     = "https://api.frankfurter.app"
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
