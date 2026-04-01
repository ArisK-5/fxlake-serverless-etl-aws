resource "aws_dynamodb_table" "pipeline_state" {
  name         = var.dynamodb_state_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pipeline_id"
  range_key    = "source"

  attribute {
    name = "pipeline_id"
    type = "S"
  }

  attribute {
    name = "source"
    type = "S"
  }

  tags = {
    Project   = "fxlake"
    ManagedBy = "terraform"
  }
}
