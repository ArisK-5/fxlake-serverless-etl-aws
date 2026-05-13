resource "aws_s3_bucket" "raw" {
  bucket        = var.raw_bucket_name
  force_destroy = true

  tags = {
    component = "storage"
    layer     = "raw"
  }
}

resource "aws_s3_bucket" "processed" {
  bucket        = var.processed_bucket_name
  force_destroy = true

  tags = {
    component = "storage"
    layer     = "processed"
  }
}

resource "aws_s3_bucket" "athena_results" {
  bucket        = var.athena_results_bucket_name
  force_destroy = true

  tags = {
    component = "storage"
    layer     = "query-results"
  }
}

resource "aws_s3_bucket" "cloudtrail_logs" {
  bucket        = var.cloudtrail_logs_bucket_name
  force_destroy = true

  tags = {
    component = "storage"
    layer     = "audit"
  }
}

resource "aws_s3_bucket" "quarantine" {
  bucket        = var.quarantine_bucket_name
  force_destroy = true

  tags = {
    component = "storage"
    layer     = "quarantine"
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail_logs" {
  bucket = aws_s3_bucket.cloudtrail_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "quarantine" {
  bucket = aws_s3_bucket.quarantine.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket = aws_s3_bucket.raw.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "processed" {
  bucket = aws_s3_bucket.processed.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "athena_results" {
  bucket = aws_s3_bucket.athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "processed_deny_unencrypted" {
  bucket = aws_s3_bucket.processed.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid       = "DenyUnencryptedObjectUploads",
        Effect    = "Deny",
        Principal = "*",
        Action    = "s3:PutObject",
        Resource  = "${aws_s3_bucket.processed.arn}/*",
        Condition = {
          StringNotEquals = {
            "s3:x-amz-server-side-encryption" = "AES256"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.processed]
}

resource "aws_s3_bucket_policy" "raw_deny_non_ssl" {
  bucket = aws_s3_bucket.raw.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid       = "DenyNonSSLRequests",
        Effect    = "Deny",
        Principal = "*",
        Action    = "s3:*",
        Resource = [
          aws_s3_bucket.raw.arn,
          "${aws_s3_bucket.raw.arn}/*"
        ],
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid       = "DenyUnencryptedObjectUploads",
        Effect    = "Deny",
        Principal = "*",
        Action    = "s3:PutObject",
        Resource  = "${aws_s3_bucket.raw.arn}/*",
        Condition = {
          StringNotEquals = {
            "s3:x-amz-server-side-encryption" = "AES256"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.raw]
}

resource "aws_s3_bucket_policy" "quarantine_restrict" {
  bucket = aws_s3_bucket.quarantine.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "AllowPipelineRoleOnly",
        Effect = "Allow",
        Principal = {
          AWS = aws_iam_role.lambda_exec.arn
        },
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ],
        Resource = [
          aws_s3_bucket.quarantine.arn,
          "${aws_s3_bucket.quarantine.arn}/*"
        ]
      },
      {
        Sid       = "DenyAllOtherPrincipals",
        Effect    = "Deny",
        Principal = "*",
        Action    = "s3:*",
        Resource = [
          aws_s3_bucket.quarantine.arn,
          "${aws_s3_bucket.quarantine.arn}/*"
        ],
        Condition = {
          StringNotEquals = {
            "aws:PrincipalArn" = aws_iam_role.lambda_exec.arn
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.quarantine]
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
