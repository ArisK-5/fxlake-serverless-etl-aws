# -----------------------------------
# FXLake - Serverless ETL Makefile
# Simplifies deployment, teardown, and maintenance
# -----------------------------------

# Variables
TF_DIR=terraform
LAMBDA_DIR=lambda
LAMBDA_ZIP=lambda_fx_ingestion.zip

# Colors for output
GREEN=\033[0;32m
RED=\033[0;31m
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
	@echo "  make backfill START=YYYY-MM-DD END=YYYY-MM-DD - Run historical backfill"
	@echo "  make validate-iceberg - Validate Iceberg table data integrity"
	@echo "  make replay-dlq    - Replay failed executions from DLQ"
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
# Backfill
# -----------------------------------
backfill:
ifndef START
	$(error START is required. Usage: make backfill START=2023-01-01 END=2023-12-31)
endif
ifndef END
	$(error END is required. Usage: make backfill START=2023-01-01 END=2023-12-31)
endif
	@echo "$(YELLOW)Starting backfill: $(START) to $(END)...$(NC)"
	$(eval SFN_ARN := $(shell cd $(TF_DIR) && terraform output -raw step_function_arn 2>&1))
	@if [ -z "$(SFN_ARN)" ] || echo "$(SFN_ARN)" | grep -qi "error"; then \
		echo "$(RED)ERROR: Failed to retrieve Step Function ARN from Terraform.$(NC)"; \
		echo "$(RED)Run 'make init' and 'make deploy' first.$(NC)"; \
		exit 1; \
	fi
	aws stepfunctions start-execution \
		--state-machine-arn "$(SFN_ARN)" \
		--input '{"mode":"backfill","start_date":"$(START)","end_date":"$(END)"}' \
		--name "backfill-$(START)-to-$(END)-$$(date +%s)" \
	|| (echo "$(RED)ERROR: Failed to start backfill execution.$(NC)" && exit 1)
	@echo "$(GREEN)Backfill execution started.$(NC)"

# -----------------------------------
# Iceberg Validation
# -----------------------------------
validate-iceberg:
	@echo "$(YELLOW)Running Iceberg data validation...$(NC)"
	$(eval VALIDATOR_NAME := $(shell cd $(TF_DIR) && terraform output -raw data_validator_function_name 2>/dev/null))
	@if [ -z "$(VALIDATOR_NAME)" ]; then \
		echo "$(RED)ERROR: Run 'make deploy' first to provision the data validator Lambda.$(NC)"; exit 1; \
	fi
	aws lambda invoke \
		--function-name "$(VALIDATOR_NAME)" \
		--payload '{}' \
		--cli-binary-format raw-in-base64-out \
		/dev/stdout 2>/dev/null | python3 -m json.tool
	@echo "$(GREEN)Iceberg validation complete.$(NC)"

# -----------------------------------
# DLQ Replay
# -----------------------------------
replay-dlq:
	@echo "$(YELLOW)Replaying messages from DLQ...$(NC)"
	$(eval QUEUE_URL := $(shell cd $(TF_DIR) && terraform output -raw dlq_url 2>/dev/null))
	$(eval SFN_ARN := $(shell cd $(TF_DIR) && terraform output -raw step_function_arn 2>/dev/null))
	@if [ -z "$(QUEUE_URL)" ] || [ -z "$(SFN_ARN)" ]; then \
		echo "$(RED)ERROR: Run 'make deploy' first to provision DLQ.$(NC)"; exit 1; \
	fi
	uv run scripts/replay_dlq.py \
		--queue-url "$(QUEUE_URL)" \
		--state-machine-arn "$(SFN_ARN)"
	@echo "$(GREEN)DLQ replay complete.$(NC)"

# -----------------------------------
# Utility Commands
# -----------------------------------
clean:
	@echo "$(YELLOW)Cleaning up...$(NC)"
	rm -f $(LAMBDA_DIR)/lambda_fx_ingestion.zip $(LAMBDA_DIR)/lambda_ecb_ingestion.zip $(LAMBDA_DIR)/lambda_fred_ingestion.zip $(LAMBDA_DIR)/lambda_validation_function.zip $(LAMBDA_DIR)/lambda_data_validator.zip
	cd $(TF_DIR) && rm -rf .terraform .terraform.lock.hcl terraform.tfstate terraform.tfstate.backup
	@echo "$(GREEN)Local cleanup complete.$(NC)"