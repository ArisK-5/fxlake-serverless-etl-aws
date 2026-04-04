# Remote state backend — S3 with DynamoDB locking.
#
# Prerequisites:
#   1. Run terraform/bootstrap/ first to create the S3 bucket and DynamoDB table
#   2. Uncomment the backend block below
#   3. Run: terraform init -migrate-state
#
# The bootstrap creates:
#   - S3 bucket with versioning, KMS encryption, and public access block
#   - DynamoDB table with LockID partition key for state locking
#
# To revert to local state:
#   terraform init -migrate-state -backend-config="path=terraform.tfstate"

# terraform {
#   backend "s3" {
#     bucket         = "fxlake-tfstate-ACCOUNT_ID"
#     key            = "fxlake/terraform.tfstate"
#     region         = "us-east-1"
#     dynamodb_table = "fxlake-tfstate-lock"
#     encrypt        = true
#   }
# }
