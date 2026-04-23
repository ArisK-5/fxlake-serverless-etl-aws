provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "fxlake"
      environment = "production"
      managed_by  = "terraform"
    }
  }
}
