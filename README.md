# FXLake — Serverless ETL on AWS

This repository provides a deployable scaffold for a **serverless, event-driven, and cost-efficient ETL pipeline** on AWS. It automates ingestion, transformation, and validation of financial exchange (FX) data using modern cloud-native services.

**Technologies used:**  
Terraform · S3 · Lambda · Glue · Athena · Step Functions · EventBridge · IAM · SNS · CloudWatch · CloudTrail · Git · Python (Polars)

---

## Table of Contents

- [📖 Overview](#📖-overview)
  - [📁 Repo Structure](#📁-repo-structure)
  - [☁️ Cloud Architecture](#☁️-cloud-architecture)
  - [🛠 Development Workflow](#🛠-development-workflow)
  - [✨ Features](#✨-features)
  - [🧠 Skills Demonstrated](#🧠-skills-demonstrated)
- [⚙️ Getting Started](#⚙️-getting-started)
  - [Prerequisites](#prerequisites)
  - [Setup & Deployment](#setup--deployment)
- [🚀 Future Improvements](#🚀-future-improvements)

## 📖 Overview

### 📁 Repo Structure

```bash
.
├── .gitignore
├── LICENSE
├── Makefile
├── README.md
├── assets
│   ├── cloud-architecture.py
│   ├── dev-workflow.py
│   ├── diagrams
│   │   ├── cloud-architecture.png
│   │   └── dev-workflow.png
│   └── icons
│       ├── dashboard.png
│       └── dev.jpg
├── glue
│   └── glue_transform.py
├── lambda
│   ├── lambda_ingestion_function.py
│   ├── lambda_validation_function.py
│   ├── package_lambdas.sh
│   └── requirements.txt
└── terraform
    ├── athena.tf
    ├── glue.tf
    ├── iam.tf
    ├── lambda.tf
    ├── monitoring.tf
    ├── outputs.tf
    ├── providers.tf
    ├── s3.tf
    ├── security.tf
    ├── step_function.tf
    ├── terraform.tfvars.example
    └── variables.tf
```

### ☁️ Cloud Architecture

The pipeline orchestrates a serverless ETL flow using AWS services:

- **AWS Lambda** (Python) fetches exchange rates from the [Frankfurter API](https://frankfurter.dev)￼ and stores raw JSON data in S3. It also performs validation by running sample Athena queries.
- **AWS Glue** (Python Shell with Polars) processes the raw JSON by flattening the rates into tabular format, then writes Parquet/CSV files to a processed S3 bucket.
- **Amazon Athena** queries the transformed data for analysis and validation purposes.
- **AWS Step Functions** coordinate the workflow steps: Lambda (extract) → Glue (transform/load) → Athena (query) → Lambda (validation).
- **Amazon EventBridge** triggers the pipeline execution on a daily schedule.
- **Amazon CloudWatch** and **SNS** provide monitoring, logging, and alarm notifications for failures or anomalies.
- **AWS IAM** ensures secure, least-privilege access, while **CloudTrail** records all API activity for auditing.
- **Terraform** manages all infrastructure provisioning as code for repeatability and version control.

The architectural diagrams of the project were made using [Diagrams](https://diagrams.mingrammer.com) in [cloud-architecture.py](assets/cloud-architecture.py) and [dev-workflow.py](assets/dev-workflow.py).

![FXLake — Serverless ETL on AWS](/assets/diagrams/cloud-architecture.png "cloud architecture diagram of the project")

### 🛠 Development Workflow

![FXLake — Serverless ETL on AWS](/assets/diagrams/dev-workflow.png "development workflow diagram")

### ✨ Features

- **Serverless & Cost-Efficient:** Fully managed pipeline with no EC2 overhead, paying only for what you use.
- **Automated Orchestration:** Step Functions coordinate the complete ETL workflow from ingestion to validation.
- **Event-Driven & Scalable:** Triggered daily via EventBridge, leveraging Glue and Polars for efficient data processing.
- **Robust Monitoring & Alerts:** CloudWatch alarms and SNS notifications ensure pipeline health and rapid incident response.
- **Secure & Auditable:** Implements IAM least-privilege access and CloudTrail for thorough auditing.
- **Extensible & Maintainable:** Easy to add new data sources or transformations, with infrastructure managed via Terraform.

### 🧠 Skills Demonstrated

- **Cloud Architecture Design**: Building scalable, fault-tolerant pipelines using AWS managed services.
- **Serverless Development**: Writing Python Lambdas and Glue jobs optimized for event-driven ETL workflows.
- **Data Engineering**: Efficiently transforming and querying data using modern tools like Polars and Athena.
- **Infrastructure Automation**: Defining and managing AWS resources with Terraform for reproducible environments.
- **Security Best Practices**: Implementing fine-grained IAM policies and auditing with CloudTrail.
- **Monitoring & Alerting Setup**: Configuring CloudWatch and SNS for real-time pipeline health tracking.
- **Version Control & Collaboration**: Using Git and GitHub for code management and team workflows.

## ⚙️ Getting Started

### Prerequisites

Ensure your development environment has the following:

- **Operating System**: macOS or Linux (Windows WSL also supported)
- **AWS CLI**: Installed and configured with proper AWS credentials and permissions
- **Terraform**: Version 1.0+ (install via Terraform downloads￼ or Homebrew)
- **Python**: Version 3.10+. I used [uv](https://docs.astral.sh/uv/#highlights) for Python environment and dependency management during development but you should be able to deploy the project without using Python.
- **Git**: For version control
- **Make**: Installed to run provided Makefile targets (usually pre-installed on macOS/Linux)
- **VSCode (optional)**: Recommended IDE with Terraform and Python extensions

### Setup & Deployment:

1. Clone the repository:

```bash
git clone https://github.com/ArisK-5/fxlake-serverless-etl-aws.git
cd fxlake-serverless-etl-aws
```

2. Configure Terraform variables

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Open terraform/terraform.tfvars and replace the required values to avoid deployment conflicts.

3. Package Lambda functions:

```bash
make package # packages Python Lambdas into deployable zip files.
```

4. Initialize Terraform backend and providers:

```bash
make init
```

5. Preview planned infrastructure changes:

```bash
make plan
```

6. Deploy infrastructure and Lambda packages:

```bash
make deploy
```

7. (Optional) Run Step Functions manually or wait for the daily scheduled EventBridge trigger:

   - You can start an execution of the deployed Step Function via the AWS Console or AWS CLI.
   - Otherwise, the pipeline runs automatically daily as configured by EventBridge.

8. Tear down all deployed resources:

```bash
make destroy # optional but recommended to save costs when you're done.
```

9. (Optional) Clean up local build artifacts and Terraform state:

```bash
make clean
```

## 🚀 Future Improvements

- Use a more rich data source.
- Add CI/CD with GitHub Actions.
- Introduce parameterization for environment-specific deployments (dev, staging, prod).
