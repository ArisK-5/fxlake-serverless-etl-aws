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

output "glue_job_name" {
  value = aws_glue_job.transform.name
}

output "step_function_arn" {
  value = aws_sfn_state_machine.etl.arn
}

output "dlq_url" {
  description = "URL of the DLQ SQS queue"
  value       = aws_sqs_queue.pipeline_dlq.url
}

output "dlq_arn" {
  description = "ARN of the DLQ SQS queue"
  value       = aws_sqs_queue.pipeline_dlq.arn
}
