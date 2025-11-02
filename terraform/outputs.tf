output "raw_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "processed_bucket" {
  value = aws_s3_bucket.processed.bucket
}

output "results_bucket" {
  value = aws_s3_bucket.athena_results.bucket
}

output "lambda_ingestion_name" {
  value = aws_lambda_function.api_ingest.function_name
}

output "glue_job_name" {
  value = aws_glue_job.transform.name
}

output "step_function_arn" {
  value = aws_sfn_state_machine.etl.arn
}
