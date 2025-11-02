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
