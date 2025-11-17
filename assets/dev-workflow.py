from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.custom import Custom
from diagrams.onprem.iac import Terraform
from diagrams.onprem.vcs import Git, Github

# Project directories
ROOT_DIR = Path(__file__).resolve().parent.parent
ICONS_DIR = ROOT_DIR / "assets/icons"
DIAGRAMS_DIR = ROOT_DIR / "assets/diagrams"

with Diagram(
    "FXLake: Serverless ETL on AWS\n(development workflow)",
    filename=str(DIAGRAMS_DIR / "dev-workflow"),
    show=False,
    direction="LR",
    graph_attr={"size": "12,8"},
):
    # Developer environment
    dev = Custom("Developer", str(ICONS_DIR / "dev.jpg"))
    git = Git("Git\n(Version Control)")
    github = Github("GitHub\n(Repository Hosting)")
    terraform = Terraform("(Infrastructure as Code)")

    dev >> Edge(label="Develop") >> git  # Developer pushes to version control

    (
        git >> Edge(label="Pull") >> github >> Edge(label="Push") >> git
    )  # Version control interaction with GitHub

    git >> Edge(label="Trigger") >> terraform  # Trigger IaC on code push

    with Cluster("AWS Cloud"):
        aws_cloud = Custom(
            "", str(ICONS_DIR / "aws.png")
        )  # Intentionally left blank label and missing icon for AWS cluster representation
        terraform >> Edge(label="provision") >> aws_cloud  # Provision AWS resources
