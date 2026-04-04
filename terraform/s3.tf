resource "aws_s3_bucket" "raw" {
  bucket        = var.raw_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "processed" {
  bucket        = var.processed_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "athena_results" {
  bucket        = var.athena_results_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "cloudtrail_logs" {
  bucket        = var.cloudtrail_logs_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket" "quarantine" {
  bucket        = var.quarantine_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "quarantine" {
  bucket = aws_s3_bucket.quarantine.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "athena_results_lifecycle" {
  bucket = aws_s3_bucket.athena_results.id

  rule {
    id     = "delete-daily"
    status = "Enabled"

    expiration {
      days = 1
    }

    filter {
      prefix = "results/"
    }
  }
}
