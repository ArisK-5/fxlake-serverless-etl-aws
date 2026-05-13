"""Tests for IAM policy document structure and permission correctness."""

import json
import re
from pathlib import Path

import pytest

TERRAFORM_DIR = Path(__file__).resolve().parent.parent / "terraform"


def _extract_jsonencode_blocks(tf_file: Path) -> dict[str, str]:
    """Extract named jsonencode() policy blocks from a Terraform file.

    Returns a dict mapping resource name to the JSON string inside jsonencode().
    """
    content = tf_file.read_text()
    results: dict[str, str] = {}

    resource_pattern = re.compile(
        r'resource\s+"aws_iam_policy"\s+"(\w+)".*?'
        r"policy\s*=\s*jsonencode\((\{.*?\})\s*\)",
        re.DOTALL,
    )
    for match in resource_pattern.finditer(content):
        name = match.group(1)
        raw_json = match.group(2)
        cleaned = _hcl_to_json(raw_json)
        results[name] = cleaned

    return results


def _extract_bucket_policy_blocks(tf_file: Path) -> dict[str, str]:
    """Extract bucket policy jsonencode() blocks from a Terraform file.

    Returns a dict mapping resource name to the JSON string inside jsonencode().
    """
    content = tf_file.read_text()
    results: dict[str, str] = {}

    pattern = re.compile(
        r'resource\s+"aws_s3_bucket_policy"\s+"(\w+)".*?'
        r"policy\s*=\s*jsonencode\((\{.*?\})\s*\)",
        re.DOTALL,
    )
    for match in pattern.finditer(content):
        name = match.group(1)
        raw_json = match.group(2)
        cleaned = _hcl_to_json(raw_json)
        results[name] = cleaned

    return results


def _hcl_to_json(hcl_block: str) -> str:
    """Best-effort HCL jsonencode block to valid JSON.

    Handles Terraform interpolations by replacing them with placeholder strings.
    """
    result = re.sub(r"\$\{[^}]+\}", "INTERPOLATED", hcl_block)
    result = re.sub(
        r"(?<!\")(?:aws_\w+\.\w+\.\w+|module\.\w+\.\w+|var\.\w+|data\.\w+\.\w+\.\w+)(?!\")",
        r'"INTERPOLATED"',
        result,
    )
    result = re.sub(r"^(\s*)(\w+)\s*=", r'\1"\2" :', result, flags=re.MULTILINE)
    result = result.replace(",]", "]").replace(",}", "}")
    result = re.sub(r"(?<=[\]\}\"])\s*\n\s*(?=[\"{\[])", ",\n", result)
    return result


@pytest.fixture(scope="module")
def iam_policies() -> dict[str, dict]:
    """Load and parse consumer IAM policies from terraform/iam.tf."""
    raw = _extract_jsonencode_blocks(TERRAFORM_DIR / "iam.tf")
    parsed: dict[str, dict] = {}
    for name, json_str in raw.items():
        if name.startswith("consumer_"):
            try:
                parsed[name] = json.loads(json_str)
            except json.JSONDecodeError:
                pytest.skip(f"Could not parse {name} policy (HCL interpolation)")
    return parsed


@pytest.fixture(scope="module")
def bucket_policies() -> dict[str, dict]:
    """Load and parse S3 bucket policies from terraform/s3.tf."""
    raw = _extract_bucket_policy_blocks(TERRAFORM_DIR / "s3.tf")
    parsed: dict[str, dict] = {}
    for name, json_str in raw.items():
        try:
            parsed[name] = json.loads(json_str)
        except json.JSONDecodeError:
            pytest.skip(f"Could not parse {name} bucket policy (HCL interpolation)")
    return parsed


# ---------------------------------------------------------------------------
# IAM policy document structure
# ---------------------------------------------------------------------------
class TestIAMPolicyStructure:
    """Verify consumer policies follow IAM policy document specification."""

    CONSUMER_POLICIES = ("consumer_readonly", "consumer_analyst", "consumer_admin")

    def test_consumer_policies_exist_in_iam_tf(self):
        content = (TERRAFORM_DIR / "iam.tf").read_text()
        for name in self.CONSUMER_POLICIES:
            assert f'"{name}"' in content, f"Missing policy: {name}"

    def test_policy_version(self, iam_policies):
        for name, policy in iam_policies.items():
            assert policy.get("Version") == "2012-10-17", f"{name} wrong Version"

    def test_policy_has_statements(self, iam_policies):
        for name, policy in iam_policies.items():
            stmts = policy.get("Statement", [])
            assert isinstance(stmts, list), f"{name} Statement not a list"
            assert len(stmts) > 0, f"{name} has no statements"

    def test_every_statement_has_effect(self, iam_policies):
        for name, policy in iam_policies.items():
            for i, stmt in enumerate(policy["Statement"]):
                assert stmt.get("Effect") in (
                    "Allow",
                    "Deny",
                ), f"{name} stmt {i} missing valid Effect"

    def test_every_statement_has_action(self, iam_policies):
        for name, policy in iam_policies.items():
            for i, stmt in enumerate(policy["Statement"]):
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                assert len(actions) > 0, f"{name} stmt {i} has no actions"

    def test_every_statement_has_resource(self, iam_policies):
        for name, policy in iam_policies.items():
            for i, stmt in enumerate(policy["Statement"]):
                resource = stmt.get("Resource")
                assert resource is not None, f"{name} stmt {i} missing Resource"

    def test_all_consumer_policies_parsed(self, iam_policies):
        expected = {"consumer_readonly", "consumer_analyst", "consumer_admin"}
        parsed = {k for k in iam_policies if k.startswith("consumer_")}
        assert parsed == expected, f"Failed to parse: {expected - parsed}"

    def test_no_wildcard_actions(self, iam_policies):
        dangerous = {"*", "s3:*", "athena:*", "lambda:*", "states:*", "glue:*"}
        for name, policy in iam_policies.items():
            all_actions = _collect_actions(policy)
            found = all_actions & dangerous
            assert not found, f"{name} has wildcard actions: {found}"


# ---------------------------------------------------------------------------
# Consumer policy permissions
# ---------------------------------------------------------------------------
class TestConsumerReadonly:
    def test_has_athena_actions(self, iam_policies):
        if "consumer_readonly" not in iam_policies:
            pytest.skip("Could not parse consumer_readonly")
        policy = iam_policies["consumer_readonly"]
        all_actions = _collect_actions(policy)
        assert "athena:StartQueryExecution" in all_actions
        assert "athena:GetQueryResults" in all_actions

    def test_has_s3_read_actions(self, iam_policies):
        if "consumer_readonly" not in iam_policies:
            pytest.skip("Could not parse consumer_readonly")
        policy = iam_policies["consumer_readonly"]
        all_actions = _collect_actions(policy)
        assert "s3:GetObject" in all_actions

    def test_has_no_s3_write_actions(self, iam_policies):
        if "consumer_readonly" not in iam_policies:
            pytest.skip("Could not parse consumer_readonly")
        policy = iam_policies["consumer_readonly"]
        processed_stmts = [
            s
            for s in policy["Statement"]
            if s.get("Sid") == "S3ProcessedRead"
        ]
        for stmt in processed_stmts:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert "s3:PutObject" not in actions
            assert "s3:DeleteObject" not in actions

    def test_has_glue_read_actions(self, iam_policies):
        if "consumer_readonly" not in iam_policies:
            pytest.skip("Could not parse consumer_readonly")
        policy = iam_policies["consumer_readonly"]
        all_actions = _collect_actions(policy)
        assert "glue:GetTable" in all_actions


class TestConsumerAnalyst:
    def test_has_athena_actions(self, iam_policies):
        if "consumer_analyst" not in iam_policies:
            pytest.skip("Could not parse consumer_analyst")
        policy = iam_policies["consumer_analyst"]
        all_actions = _collect_actions(policy)
        assert "athena:StartQueryExecution" in all_actions

    def test_has_no_processed_s3_read(self, iam_policies):
        if "consumer_analyst" not in iam_policies:
            pytest.skip("Could not parse consumer_analyst")
        policy = iam_policies["consumer_analyst"]
        sids = [s.get("Sid", "") for s in policy["Statement"]]
        assert "S3ProcessedRead" not in sids, (
            "Analyst should not have direct S3 read on processed bucket"
        )

    def test_has_athena_results_access(self, iam_policies):
        if "consumer_analyst" not in iam_policies:
            pytest.skip("Could not parse consumer_analyst")
        policy = iam_policies["consumer_analyst"]
        sids = [s.get("Sid", "") for s in policy["Statement"]]
        assert "S3AthenaResultsOnly" in sids


class TestConsumerAdmin:
    def test_has_step_functions_control(self, iam_policies):
        if "consumer_admin" not in iam_policies:
            pytest.skip("Could not parse consumer_admin")
        policy = iam_policies["consumer_admin"]
        all_actions = _collect_actions(policy)
        assert "states:StartExecution" in all_actions
        assert "states:StopExecution" in all_actions

    def test_has_lambda_invoke(self, iam_policies):
        if "consumer_admin" not in iam_policies:
            pytest.skip("Could not parse consumer_admin")
        policy = iam_policies["consumer_admin"]
        all_actions = _collect_actions(policy)
        assert "lambda:InvokeFunction" in all_actions

    def test_has_s3_write_access(self, iam_policies):
        if "consumer_admin" not in iam_policies:
            pytest.skip("Could not parse consumer_admin")
        policy = iam_policies["consumer_admin"]
        all_actions = _collect_actions(policy)
        assert "s3:PutObject" in all_actions
        assert "s3:DeleteObject" in all_actions

    def test_has_cloudwatch_read(self, iam_policies):
        if "consumer_admin" not in iam_policies:
            pytest.skip("Could not parse consumer_admin")
        policy = iam_policies["consumer_admin"]
        all_actions = _collect_actions(policy)
        assert "cloudwatch:GetMetricData" in all_actions


# ---------------------------------------------------------------------------
# S3 bucket policies
# ---------------------------------------------------------------------------
class TestBucketPolicies:
    def test_bucket_policies_exist_in_s3_tf(self):
        content = (TERRAFORM_DIR / "s3.tf").read_text()
        for name in (
            "processed_deny_unencrypted",
            "raw_deny_non_ssl",
            "quarantine_restrict",
        ):
            assert f'"{name}"' in content, f"Missing bucket policy: {name}"

    def test_processed_denies_unencrypted(self):
        content = (TERRAFORM_DIR / "s3.tf").read_text()
        assert "DenyUnencryptedObjectUploads" in content
        assert "s3:x-amz-server-side-encryption" in content

    def test_raw_denies_non_ssl(self):
        content = (TERRAFORM_DIR / "s3.tf").read_text()
        assert "DenyNonSSLRequests" in content
        assert "aws:SecureTransport" in content

    def test_quarantine_restricts_to_pipeline_role(self):
        content = (TERRAFORM_DIR / "s3.tf").read_text()
        assert "AllowPipelineRoleOnly" in content
        assert "DenyAllOtherPrincipals" in content

    def test_processed_deny_condition_uses_string_not_equals(self, bucket_policies):
        if "processed_deny_unencrypted" not in bucket_policies:
            pytest.skip("Could not parse processed bucket policy")
        policy = bucket_policies["processed_deny_unencrypted"]
        deny_stmts = [s for s in policy["Statement"] if s.get("Effect") == "Deny"]
        assert len(deny_stmts) >= 1
        condition = deny_stmts[0].get("Condition", {})
        assert "StringNotEquals" in condition
        sse = condition["StringNotEquals"]
        assert sse.get("s3:x-amz-server-side-encryption") == "AES256"

    def test_raw_deny_condition_uses_secure_transport(self, bucket_policies):
        if "raw_deny_non_ssl" not in bucket_policies:
            pytest.skip("Could not parse raw bucket policy")
        policy = bucket_policies["raw_deny_non_ssl"]
        deny_stmts = [s for s in policy["Statement"] if s.get("Sid") == "DenyNonSSLRequests"]
        assert len(deny_stmts) == 1
        condition = deny_stmts[0].get("Condition", {})
        assert "Bool" in condition
        assert condition["Bool"].get("aws:SecureTransport") == "false"

    def test_quarantine_deny_condition_uses_string_not_equals(self, bucket_policies):
        if "quarantine_restrict" not in bucket_policies:
            pytest.skip("Could not parse quarantine bucket policy")
        policy = bucket_policies["quarantine_restrict"]
        deny_stmts = [
            s for s in policy["Statement"]
            if s.get("Sid") == "DenyAllOtherPrincipals"
        ]
        assert len(deny_stmts) == 1
        condition = deny_stmts[0].get("Condition", {})
        assert "StringNotEquals" in condition
        assert "aws:PrincipalArn" in condition["StringNotEquals"]


# ---------------------------------------------------------------------------
# CloudTrail & access logging
# ---------------------------------------------------------------------------
class TestCloudTrailConfig:
    def test_event_selector_exists(self):
        content = (TERRAFORM_DIR / "security.tf").read_text()
        assert "event_selector" in content

    def test_s3_data_events_configured(self):
        content = (TERRAFORM_DIR / "security.tf").read_text()
        assert "AWS::S3::Object" in content

    def test_insight_selector_exists(self):
        content = (TERRAFORM_DIR / "security.tf").read_text()
        assert "insight_selector" in content
        assert "ApiCallRateInsight" in content

    def test_s3_access_logging_processed(self):
        content = (TERRAFORM_DIR / "security.tf").read_text()
        assert "s3-access-logs/processed/" in content

    def test_s3_access_logging_raw(self):
        content = (TERRAFORM_DIR / "security.tf").read_text()
        assert "s3-access-logs/raw/" in content


# ---------------------------------------------------------------------------
# Public access blocks
# ---------------------------------------------------------------------------
class TestPublicAccessBlocks:
    @pytest.mark.parametrize(
        "bucket", ["raw", "processed", "athena_results", "quarantine", "cloudtrail_logs"],
    )
    def test_public_access_block_exists(self, bucket):
        content = (TERRAFORM_DIR / "s3.tf").read_text()
        pattern = rf'resource\s+"aws_s3_bucket_public_access_block"\s+"{bucket}"'
        assert re.search(pattern, content), (
            f"Missing public access block for {bucket}"
        )


# ---------------------------------------------------------------------------
# Audit query template
# ---------------------------------------------------------------------------
class TestAuditQueryTemplate:
    QUERY_FILE = Path(__file__).resolve().parent.parent / "docs" / "queries" / "audit_trail.sql"

    def test_file_exists(self):
        assert self.QUERY_FILE.is_file()

    def test_contains_s3_access_query(self):
        content = self.QUERY_FILE.read_text()
        assert "s3.amazonaws.com" in content

    def test_contains_failed_access_query(self):
        content = self.QUERY_FILE.read_text()
        assert "AccessDenied" in content

    def test_contains_pipeline_execution_query(self):
        content = self.QUERY_FILE.read_text()
        assert "states.amazonaws.com" in content

    def test_contains_data_modification_query(self):
        content = self.QUERY_FILE.read_text()
        assert "PutObject" in content
        assert "DeleteObject" in content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _collect_actions(policy: dict) -> set[str]:
    """Collect all action strings from a policy document."""
    actions: set[str] = set()
    for stmt in policy.get("Statement", []):
        raw = stmt.get("Action", [])
        if isinstance(raw, str):
            raw = [raw]
        actions.update(raw)
    return actions
