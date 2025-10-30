# Glue Script location (we rely on developer to upload glue/glue_transform.py to S3)
resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.raw.bucket
  key    = var.glue_script_s3_key
  source = "../glue/glue_transform.py"
}

resource "aws_glue_job" "transform" {
  name     = var.glue_job_name
  role_arn = aws_iam_role.glue_service_role.arn

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_bucket.raw.id}/${var.glue_script_s3_key}"
  }

  max_retries  = 0
  max_capacity = 2
  glue_version = "3.0"
  default_arguments = {
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.id}/tmp/"
    "--job-language"                     = "python"
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.id
    "--PROCESSED_BUCKET"                 = aws_s3_bucket.processed.id
    "--OUTPUT_FORMAT"                    = var.fx_output_format
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-metrics"                   = "true"
  }
}
