resource "aws_glue_job" "transform" {
  name     = var.glue_job_name
  role_arn = aws_iam_role.glue_service_role.arn

  command {
    name            = "pythonshell"
    script_location = "s3://${aws_s3_bucket.processed.bucket}/${var.glue_script_s3_key}"
    python_version  = "3.9"
  }

  glue_version = "3.0"

  max_capacity = 0.0625 # 0.0625 or 1

  max_retries = 0

  default_arguments = {
    "--RAW_BUCKET"                       = aws_s3_bucket.raw.bucket
    "--PROCESSED_BUCKET"                 = aws_s3_bucket.processed.bucket
    "--OUTPUT_FORMAT"                    = var.fx_output_format
    "--LOG_LEVEL"                        = "INFO"
    "--enable-continuous-cloudwatch-log" = "true"
    "--enable-metrics"                   = "true"

    "--additional-python-modules" = "polars==0.18.8,boto3,pyarrow"
  }
}

resource "aws_s3_object" "glue_script" {
  bucket      = aws_s3_bucket.processed.bucket
  key         = var.glue_script_s3_key
  source      = "../glue/glue_transform.py"
  source_hash = filebase64sha256("../glue/glue_transform.py")
}
