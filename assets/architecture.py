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
from diagrams.onprem.vcs import Git, Github

# Project directories
ROOT_DIR = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT_DIR / "assets/icons"
DIAGRAMS_DIR = ROOT_DIR / "assets/diagrams"

with Diagram(
    "FXLake: Serverless ETL on AWS\n(pipeline architecture)",
    filename=str(DIAGRAMS_DIR / "architecture"),
    show=False,
    direction="LR",
):
    # Developer environment
    dev = Custom("Developer", str(ICONS_DIR / "dev.jpg"))
    git = Git("Git\n(Version Control)")
    github = Github("GitHub\n(Repository Hosting)")
    terraform = Terraform("(Infrastructure as Code)")
    api_source = APIService("Frankfurter API")

    dev >> Edge(label="Develop") >> git  # Developer pushes to version control

    (
        git >> Edge(label="Pull") >> github >> Edge(label="Push") >> git
    )  # Version control interaction with GitHub

    git >> Edge(label="Trigger") >> terraform  # Trigger IaC on code push

    with Cluster("AWS Cloud"):
        aws_cloud = Custom(
            "", str(ICONS_DIR / "aws.png")
        )  # Intentionally left blank label and icon for AWS Cloud

        with Cluster("Orchestration"):
            step_function = StepFunctions(
                "Step Functions\n(Workflow Orchestration)"
            )  # Pipeline orchestration
            eventbridge = Eventbridge(
                "EventBridge\n(Event Routing)"
            )  # Event-driven triggers

        with Cluster("ETL Pipeline"):
            lambda_function = Lambda("Lambda\n(Serverless Compute)")
            glue = Glue("Glue\n(ETL Service)")
            athena = Athena("Athena\n(Query Service)")

        with Cluster("Data Lake"):
            s3_raw = S3("S3 Raw Bucket\n(Raw Data)")
            s3_processed = S3("S3 Processed Bucket\n(Processed Data)")
            s3_athena_results = S3("S3 Athena Results\n(Sample Queries)")

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
            >> Edge(label="read")
            >> glue
            >> Edge(label="transform")
            >> s3_processed
        )  # Transform and Load data with Glue

        (athena >> Edge(label="query") >> s3_processed)
        athena >> Edge(label="store sample") >> s3_athena_results
        (
            lambda_function >> Edge(label="validate results") >> s3_athena_results
        )  # Validate results with Lambda

        with Cluster("Monitoring & Security"):
            cloudwatch = Cloudwatch("CloudWatch\n(Monitoring)")
            cloudtrail = Cloudtrail("CloudTrail\n(Audit Logs)")
            iam = IAM("IAM\n(Access Management)")
            sns = SNS("SNS\n(Notification Service)")

        sns >> Edge(label="alert") >> dev  # Notifications

        terraform >> Edge(label="provision") >> aws_cloud  # Provision AWS resources
