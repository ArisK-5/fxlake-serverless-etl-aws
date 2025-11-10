resource "aws_glue_job" "transform" {
  name     = var.glue_job_name
  role_arn = aws_iam_role.glue_service_role.arn

  command {
    name            = "glueetl"
    script_location = "s3://${aws_s3_bucket.processed.bucket}/${var.glue_script_s3_key}"
    python_version  = "3"
  }

  glue_version = "3.0"
  max_capacity = 1
  max_retries  = 0

  default_arguments = {
    "--TempDir"                          = "s3://${aws_s3_bucket.raw.bucket}/tmp/"
    "--job-language"                     = "python"
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--PROCESSED_BUCKET"                 = aws_s3_bucket.processed.bucket
    "--OUTPUT_FORMAT"                    = var.fx_output_format
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-metrics"                   = "true"
  }
}

resource "aws_s3_object" "glue_script" {
  bucket = aws_s3_bucket.processed.bucket
  key    = var.glue_script_s3_key
  source = "../glue/glue_transform.py"
}
