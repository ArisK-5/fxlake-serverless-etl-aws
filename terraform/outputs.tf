output "raw_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "processed_bucket" {
  value = aws_s3_bucket.processed.bucket
}

output "results_bucket" {
  value = aws_s3_bucket.athena_results.bucket
}

output "lambda_fx_ingestion_name" {
  value = aws_lambda_function.fx_ingest.function_name
}

output "step_function_arn" {
  value = aws_sfn_state_machine.etl.arn
}

output "lambda_iceberg_writer_name" {
  value = module.iceberg_writer.function_name
}

output "data_validator_function_name" {
  description = "Name of the data validator Lambda function"
  value       = module.data_validator.function_name
}

output "iceberg_maintenance_function_name" {
  description = "Name of the Iceberg maintenance Lambda function"
  value       = module.iceberg_maintenance.function_name
}

output "dlq_url" {
  description = "URL of the DLQ SQS queue"
  value       = aws_sqs_queue.pipeline_dlq.url
}

output "dlq_arn" {
  description = "ARN of the DLQ SQS queue"
  value       = aws_sqs_queue.pipeline_dlq.arn
}

output "codebuild_dbt_project_name" {
  description = "Name of the CodeBuild project for dbt"
  value       = aws_codebuild_project.dbt_transform.name
}

# -----------------------------------------------------------
# Data Catalog
# -----------------------------------------------------------

output "glue_database_name" {
  description = "Name of the Glue Data Catalog database"
  value       = aws_glue_catalog_database.fxlake.name
}

output "glue_database_arn" {
  description = "ARN of the Glue Data Catalog database"
  value       = aws_glue_catalog_database.fxlake.arn
}

output "glue_table_fx_rates_arn" {
  description = "ARN of the fx_rates Iceberg table in Glue Data Catalog"
  value       = aws_glue_catalog_table.fx_rates_iceberg.arn
}

output "glue_table_fx_rates_name" {
  description = "Name of the fx_rates Iceberg table"
  value       = aws_glue_catalog_table.fx_rates_iceberg.name
}

output "glue_table_economic_indicators_arn" {
  description = "ARN of the economic_indicators Iceberg table in Glue Data Catalog"
  value       = aws_glue_catalog_table.economic_indicators_iceberg.arn
}

output "glue_table_economic_indicators_name" {
  description = "Name of the economic_indicators Iceberg table"
  value       = aws_glue_catalog_table.economic_indicators_iceberg.name
}

output "athena_workgroup_name" {
  description = "Name of the Athena workgroup for queries"
  value       = aws_athena_workgroup.fxlake.name
}
