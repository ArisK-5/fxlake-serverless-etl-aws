resource "aws_s3_bucket" "raw" {
  bucket        = var.raw_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "processed" {
  bucket        = var.processed_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "athena_results" {
  bucket        = var.athena_results_bucket
  force_destroy = true
}

resource "aws_s3_bucket" "cloudtrail_logs" {
  bucket        = var.cloudtrail_logs_bucket
  force_destroy = true
}
