# FXLake — Serverless ETL on AWS

This repository contains a deployable scaffold for a serverless ETL pipeline on AWS. It automates the ingestion, transformation, and validation of financial exchange (FX) data using a **modern, event-driven, and cost-efficient architecture**.

Technologies currently in use:

Terraform · S3 · Lambda · Glue · Athena · StepFunctions · EventBridge · IAM · SNS · Cloudwatch · Cloudtrail · Git · Python (Polars)

---

## Table of Contents

- [📖 Overview](#📖-overview)
  - [📁 Repo Structure](#📁-repo-structure)
  - [☁️ Cloud Architecture](#☁️-cloud-architecture)
  - [🛠 Development Workflow](#🛠-development-workflow)
  - [✨ Features](#✨-features)
  - [🧠 Skills Demonstrated](#🧠-skills-demonstrated)
- [⚙️ Prerequisites & Setup](#⚙️-prerequisites--setup)
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

## ⚙️ Prerequisites & Setup

Before deploying, make sure you have the following:

- macOS / Linux terminal
- AWS CLI configured with credentials
- Terraform￼
- Python 3.10+￼

Quick setup:

1. Edit `terraform/terraform.tfvars.example` → save as `terraform.tfvars` and replace default bucket names with globally unique ones.
2. Prepare the Lambda package: `make package` (creates two Lambda `.zip` files)
3. `make deploy`
4. (Optional) Run Step Function execution or wait for daily scheduled EventBridge trigger.

## 🚀 Future Improvements

- Add CI/CD deployment with GitHub Actions
