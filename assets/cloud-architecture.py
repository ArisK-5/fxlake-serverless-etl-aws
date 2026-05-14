from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.devtools import Codebuild
from diagrams.aws.integration import SNS, SQS, Eventbridge, StepFunctions
from diagrams.aws.management import Cloudtrail, Cloudwatch
from diagrams.aws.security import IAM
from diagrams.aws.storage import S3
from diagrams.custom import Custom
from diagrams.oci.devops import APIService
from diagrams.onprem.iac import Terraform

# Project directories
ROOT_DIR = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT_DIR / "assets/icons"
DIAGRAMS_DIR = ROOT_DIR / "assets/diagrams"

with Diagram(
    "FXLake: Serverless ETL on AWS\n(cloud architecture)",
    filename=str(DIAGRAMS_DIR / "cloud-architecture"),
    show=False,
    direction="LR",
    graph_attr={"size": "20,12", "dpi": "240"},
):
    # External factors
    dev = Custom("Developer", str(ICONS_DIR / "dev.jpg"))
    terraform = Terraform("")

    # Data sources
    with Cluster("Data Sources"):
        frankfurter_api = APIService("Frankfurter API")
        ecb_api = APIService("ECB SDW API")
        fred_api = APIService("FRED API")

    with Cluster("AWS Cloud"):
        aws_cloud = Custom(
            "", str(ICONS_DIR / "aws.png")
        )  # Intentionally left blank label and missing icon for AWS cluster representation
        terraform >> Edge(label="provision") >> aws_cloud  # Provision AWS resources

        with Cluster("Orchestration"):
            step_function = StepFunctions("Step Functions")
            eventbridge = Eventbridge("EventBridge")

        with Cluster("ETL Pipeline"):
            lambda_fx = Lambda("FX Ingestion")
            lambda_ecb = Lambda("ECB Ingestion")
            lambda_fred = Lambda("FRED Ingestion")
            lambda_iceberg = Lambda("Iceberg Writer")
            codebuild = Codebuild("dbt Transform")
            athena = Athena("Athena\n(Iceberg)")
            lambda_validation = Lambda("Validation")
            lambda_cross = Lambda("Cross-Source\nValidation")

        with Cluster("Self-Healing"):
            dlq = SQS("Pipeline DLQ")
            lambda_dlq_retry = Lambda("DLQ Auto-Retry")
            lambda_stale = Lambda("Stale Data\nBackfill")
            eventbridge_hourly = Eventbridge("Hourly Check")

        with Cluster("State Management"):
            dynamodb = Dynamodb("DynamoDB\n(pipeline state)")

        with Cluster("Data Lake"):
            s3_raw = S3("S3 Raw")
            s3_processed = S3("S3 Processed")
            s3_athena_results = S3("S3 Athena Results")
            s3_quarantine = S3("S3 Quarantine")

        with Cluster("Monitoring & Security"):
            cloudwatch = Cloudwatch("CloudWatch")
            cloudtrail = Cloudtrail("CloudTrail")
            iam = IAM("IAM")
            sns = SNS("SNS")
            cloudwatch_dashboard = Custom(
                "Monitoring Dashboard", str(ICONS_DIR / "dashboard.png")
            )

        # Orchestration — EventBridge triggers Step Functions daily
        eventbridge >> Edge(label="daily trigger") >> step_function

        # Step Functions orchestrates the pipeline
        step_function >> Edge(label="parallel\ningestion") >> [
            lambda_fx,
            lambda_ecb,
            lambda_fred,
        ]
        step_function >> Edge(label="Iceberg\nwrite") >> lambda_iceberg
        step_function >> Edge(label="dbt\ntransform") >> codebuild
        step_function >> Edge(label="query") >> athena
        step_function >> Edge(label="validate") >> lambda_validation
        step_function >> Edge(label="cross-source") >> lambda_cross

        # Ingestion — each Lambda fetches from its API source
        frankfurter_api >> lambda_fx
        ecb_api >> lambda_ecb
        fred_api >> lambda_fred

        # Lambdas read/write DynamoDB state (incremental watermark)
        lambda_fx >> Edge(style="dashed", label="state") >> dynamodb
        lambda_ecb >> Edge(style="dashed") >> dynamodb
        lambda_fred >> Edge(style="dashed") >> dynamodb

        # Lambdas write raw JSON to S3
        [lambda_fx, lambda_ecb, lambda_fred] >> Edge(label="raw JSON") >> s3_raw

        # Iceberg writer reads raw, writes via Athena INSERT, quarantines bad data
        s3_raw >> lambda_iceberg
        lambda_iceberg >> Edge(label="Athena\nINSERT") >> athena
        lambda_iceberg >> Edge(label="quarantine", style="dashed", color="red") >> s3_quarantine

        # dbt reads from Iceberg tables, writes mart tables
        codebuild >> Edge(label="SQL models") >> athena

        # Athena queries processed data (Iceberg tables)
        s3_processed << Edge(label="Iceberg\ntables") << athena
        athena >> s3_athena_results

        # Validation Lambda reads Athena results
        lambda_validation >> s3_athena_results

        # Self-healing flows
        step_function >> Edge(label="failures", style="dashed", color="red") >> dlq
        dlq >> lambda_dlq_retry
        lambda_dlq_retry >> Edge(label="replay", style="dashed") >> step_function
        lambda_dlq_retry >> Edge(label="alert", style="dashed", color="red") >> sns
        eventbridge_hourly >> Edge(label="hourly") >> lambda_stale
        lambda_stale >> Edge(style="dashed") >> dynamodb
        lambda_stale >> Edge(label="backfill", style="dashed") >> step_function

        # Monitoring & Notifications
        cloudwatch - sns >> Edge(label="alert") >> dev
        cloudwatch >> cloudwatch_dashboard << Edge(label="monitor") << dev
