variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "pipeline" {
  type        = string
  description = "Pipeline name"
  default     = "fxlake-etl"
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

variable "quarantine_bucket_name" {
  description = "S3 bucket for quarantined data quality failures"
  type        = string
}

variable "metric_namespace_prefix" {
  description = "Prefix for CloudWatch metric namespaces"
  type        = string
  default     = "FXLake"
}

variable "sns_email_address" {
  description = "Email address for SNS notifications"
  type        = string
}

variable "lambda_fx_ingestion_name" {
  type    = string
  default = "fxlake-fx-ingest-lambda"
}

variable "lambda_ecb_ingestion_name" {
  type    = string
  default = "fxlake-ecb-ingest-lambda"
}

variable "lambda_fred_ingestion_name" {
  type    = string
  default = "fxlake-fred-ingest-lambda"
}

variable "fred_base_url" {
  description = "Base URL for the FRED API"
  type        = string
  default     = "https://api.stlouisfed.org/fred"
}

variable "fred_series" {
  description = "FRED series ID to ingest (e.g. UNRATE, FEDFUNDS)"
  type        = string
  default     = "UNRATE"
}

variable "fred_api_key" {
  description = "API key for the FRED API (stored as secret)"
  type        = string
  sensitive   = true
}

variable "ecb_base_url" {
  description = "Base URL for the ECB Statistics Data Warehouse SDMX-JSON API"
  type        = string
  default     = "https://data-api.ecb.europa.eu/service/data"
}

variable "lambda_validation_name" {
  type    = string
  default = "fxlake-results-check-lambda"
}

variable "lambda_iceberg_writer_name" {
  type    = string
  default = "fxlake-iceberg-writer-lambda"
}

variable "lambda_data_validator_name" {
  type    = string
  default = "fxlake-data-validator-lambda"
}

variable "lambda_iceberg_maintenance_name" {
  type    = string
  default = "fxlake-iceberg-maintenance-lambda"
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

variable "dynamodb_state_table_name" {
  description = "DynamoDB table name for pipeline state tracking"
  type        = string
  default     = "fxlake-pipeline-state"
}

variable "lambda_cross_validator_name" {
  type    = string
  default = "fxlake-cross-validator-lambda"
}

variable "deployer_role_arn" {
  description = "IAM role ARN used by CI/CD (GitHub Actions OIDC) to deploy infrastructure"
  type        = string
  sensitive   = true
}

