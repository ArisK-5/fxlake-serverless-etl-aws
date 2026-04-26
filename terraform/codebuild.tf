data "archive_file" "dbt_project" {
  type        = "zip"
  output_path = "${path.module}/../lambda/dbt-project.zip"
  source_dir  = "${path.module}/../dbt"

  excludes = [
    "target",
    "dbt_packages",
    "logs",
    "__pycache__",
  ]
}

resource "aws_s3_object" "dbt_project" {
  bucket = aws_s3_bucket.processed.bucket
  key    = "codebuild/dbt-project.zip"
  source = data.archive_file.dbt_project.output_path
  etag   = data.archive_file.dbt_project.output_md5

  tags = {
    component = "transform"
  }
}

resource "aws_codebuild_project" "dbt_transform" {
  name         = "fxlake-dbt-transform"
  description  = "Runs dbt models and tests against Athena/Iceberg tables"
  service_role = aws_iam_role.codebuild_dbt.arn

  build_timeout = 10

  source {
    type      = "S3"
    location  = "${aws_s3_object.dbt_project.bucket}/${aws_s3_object.dbt_project.key}"
    buildspec = "buildspec.yml"
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type         = "LINUX_CONTAINER"

    environment_variable {
      name  = "DBT_ATHENA_S3_STAGING_DIR"
      value = "s3://${aws_s3_bucket.athena_results.bucket}/dbt-staging/"
    }

    environment_variable {
      name  = "DBT_ATHENA_REGION"
      value = var.aws_region
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_dbt.name
    }
  }

  tags = {
    component = "transform"
  }
}

resource "aws_cloudwatch_log_group" "codebuild_dbt" {
  name              = "/aws/codebuild/fxlake-dbt-transform"
  retention_in_days = 14

  tags = {
    component = "monitoring"
  }
}
