# -----------------------------------
# FXLake - Serverless ETL Makefile
# Simplifies deployment, teardown, and maintenance
# -----------------------------------

# Variables
TF_DIR=terraform
LAMBDA_DIR=lambda
LAMBDA_ZIP=lambda_ingestion_function.zip

# Colors for output
GREEN=\033[0;32m
YELLOW=\033[1;33m
NC=\033[0m # No Color

# -----------------------------------
# Default target
# -----------------------------------
help:
	@echo ""
	@echo "FXLake - Serverless ETL (Terraform + Lambda + Glue + Athena)"
	@echo ""
	@echo "Available targets:"
	@echo "  make package      - Package Lambda function into a .zip"
	@echo "  make init         - Initialize Terraform backend and providers"
	@echo "  make plan         - Show infrastructure plan"
	@echo "  make deploy       - Deploy (apply) all infrastructure"
	@echo "  make destroy      - Tear down all resources"
	@echo "  make test          - Run unit tests"
	@echo "  make test-integration - Run integration tests"
	@echo "  make test-all      - Run all tests with coverage"
	@echo "  make clean        - Remove Lambda zip and Terraform cache"
	@echo ""

# -----------------------------------
# Lambda Packaging
# -----------------------------------
package:
	@echo "$(YELLOW)Packaging Lambda functions...$(NC)"
	cd $(LAMBDA_DIR) && ./package_lambdas.sh
	@echo "$(GREEN)Lambda packages created successfully!$(NC)"

# -----------------------------------
# Testing
# -----------------------------------
test:
	@echo "$(YELLOW)Running unit tests...$(NC)"
	uv run pytest tests/ -v --ignore=tests/integration
	@echo "$(GREEN)Unit tests passed.$(NC)"

test-integration:
	@echo "$(YELLOW)Running integration tests...$(NC)"
	uv run pytest tests/integration/ -v -m integration
	@echo "$(GREEN)Integration tests passed.$(NC)"

test-all:
	@echo "$(YELLOW)Running all tests with coverage...$(NC)"
	uv run pytest tests/ -v --cov=lambda --cov=glue --cov-report=term-missing
	@echo "$(GREEN)All tests passed.$(NC)"

# -----------------------------------
# Terraform Commands
# -----------------------------------
init:
	@echo "$(YELLOW)Initializing Terraform...$(NC)"
	cd $(TF_DIR) && terraform init
	@echo "$(GREEN)Terraform initialized.$(NC)"

plan:
	@echo "$(YELLOW)Creating Terraform plan...$(NC)"
	cd $(TF_DIR) && terraform plan
	@echo "$(GREEN)Plan completed.$(NC)"

deploy:
	@echo "$(YELLOW)Deploying infrastructure...$(NC)"
	cd $(TF_DIR) && terraform apply -auto-approve
	@echo "$(GREEN)Deployment complete!$(NC)"

destroy:
	@echo "$(YELLOW)Destroying infrastructure...$(NC)"
	cd $(TF_DIR) && terraform destroy -auto-approve
	@echo "$(GREEN)All infrastructure removed.$(NC)"

# -----------------------------------
# Utility Commands
# -----------------------------------
clean:
	@echo "$(YELLOW)Cleaning up...$(NC)"
	rm -f $(LAMBDA_DIR)/lambda_ingestion_function.zip $(LAMBDA_DIR)/lambda_validation_function.zip
	cd $(TF_DIR) && rm -rf .terraform .terraform.lock.hcl terraform.tfstate terraform.tfstate.backup
	@echo "$(GREEN)Local cleanup complete.$(NC)"