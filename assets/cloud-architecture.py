from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import Athena, Glue
from diagrams.aws.compute import Lambda
from diagrams.aws.integration import SNS, Eventbridge, StepFunctions
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
    graph_attr={"size": "16,10", "dpi": "240"},
):
    # External factors
    dev = Custom("Developer", str(ICONS_DIR / "dev.jpg"))
    api_source = APIService("Frankfurter API")
    terraform = Terraform("")

    with Cluster("AWS Cloud"):
        aws_cloud = Custom(
            "", str(ICONS_DIR / "aws.png")
        )  # Intentionally left blank label and missing icon for AWS cluster representation
        terraform >> Edge(label="provision") >> aws_cloud  # Provision AWS resources

        with Cluster("Orchestration"):
            step_function = StepFunctions("Step Functions")
            eventbridge = Eventbridge("EventBridge")

        with Cluster("ETL Pipeline"):
            lambda_function = Lambda("Lambda")
            glue = Glue("Glue)")
            athena = Athena("Athena")

        with Cluster("Data Lake"):
            s3_raw = S3("S3 Raw Bucket")
            s3_processed = S3("S3 Processed Bucket")
            s3_athena_results = S3("S3 Athena Results")

        with Cluster("Monitoring & Security"):
            cloudwatch = Cloudwatch("CloudWatch")
            cloudtrail = Cloudtrail("CloudTrail")
            iam = IAM("IAM")
            sns = SNS("SNS")
            cloudwatch_dashboard = Custom(
                "Monitoring Dashboard", str(ICONS_DIR / "dashboard.png")
            )

        # Orchestration
        (
            eventbridge
            >> Edge(label="daily trigger")
            >> step_function
            >> [lambda_function, glue, athena]
        )

        # Data flow
        (
            api_source >> lambda_function >> Edge(label="extract") >> s3_raw
        )  # Extract data via Lambda and Frankfurter API
        (
            s3_raw
            >> Edge(label="transform")
            >> glue
            >> Edge(label="load")
            >> s3_processed
        )  # Transform and Load data with Glue

        (
            s3_processed
            << Edge(label="query")
            << athena
            >> Edge(label="sample query results")
            >> s3_athena_results
        )  # Query processed data and store sample results

        (
            lambda_function >> Edge(label="validate results") >> s3_athena_results
        )  # Validate results with Lambda

        # Monitoring & Notifications
        (
            cloudwatch - sns >> Edge(label="alert") >> dev
        )  # Email notifications for pipeline failures

        (
            cloudwatch >> cloudwatch_dashboard << Edge(label="monitor metrics") << dev
        )  # Monitoring
