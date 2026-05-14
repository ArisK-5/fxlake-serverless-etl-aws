import json
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl",
)

from lambda_stale_data_backfill import (
    SOURCES,
    StaleSource,
    check_source_staleness,
    lambda_handler,
    trigger_backfill_execution,
)

SFN_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl"
TEST_STATE_TABLE = "test-state-table"
TODAY = date.today()


@pytest.fixture()
def dynamodb_mock():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TEST_STATE_TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pipeline_id", "AttributeType": "S"},
                {"AttributeName": "source", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                {"AttributeName": "source", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb


def _put_state(ddb, source: str, last_date: str) -> None:
    ddb.put_item(
        TableName=TEST_STATE_TABLE,
        Item={
            "pipeline_id": {"S": "fxlake"},
            "source": {"S": source},
            "last_processed_date": {"S": last_date},
        },
    )


# ---------------------------------------------------------------------------
# StaleSource dataclass
# ---------------------------------------------------------------------------
class TestStaleSource:
    def test_frozen(self):
        ss = StaleSource(
            source="frankfurter",
            last_date="2024-01-01",
            gap_days=10,
            backfill_start="2024-01-02",
            backfill_end="2024-01-11",
        )
        with pytest.raises(AttributeError):
            ss.source = "ecb"

    def test_valid(self):
        ss = StaleSource(
            source="ecb",
            last_date="2024-06-01",
            gap_days=5,
            backfill_start="2024-06-02",
            backfill_end="2024-06-06",
        )
        assert ss.source == "ecb"
        assert ss.gap_days == 5

    def test_negative_gap_rejected(self):
        with pytest.raises(ValueError, match="gap_days must be positive"):
            StaleSource(
                source="fred",
                last_date="2024-01-01",
                gap_days=-1,
                backfill_start="2024-01-02",
                backfill_end="2024-01-03",
            )


# ---------------------------------------------------------------------------
# check_source_staleness
# ---------------------------------------------------------------------------
class TestCheckSourceStaleness:
    def test_detects_stale_source(self, dynamodb_mock):
        stale_date = (TODAY - timedelta(days=5)).isoformat()
        _put_state(dynamodb_mock, "frankfurter", stale_date)

        result = check_source_staleness(
            "frankfurter", dynamodb_mock, TEST_STATE_TABLE,
            "fxlake", threshold_days=2,
        )

        assert result is not None
        assert result.source == "frankfurter"
        assert result.gap_days >= 4
        assert result.backfill_start == (TODAY - timedelta(days=4)).isoformat()
        assert result.backfill_end == TODAY.isoformat()

    def test_returns_none_for_fresh_source(self, dynamodb_mock):
        fresh_date = (TODAY - timedelta(days=1)).isoformat()
        _put_state(dynamodb_mock, "ecb", fresh_date)

        result = check_source_staleness(
            "ecb", dynamodb_mock, TEST_STATE_TABLE,
            "fxlake", threshold_days=2,
        )

        assert result is None

    def test_returns_none_for_exactly_threshold(self, dynamodb_mock):
        threshold_date = (TODAY - timedelta(days=2)).isoformat()
        _put_state(dynamodb_mock, "fred", threshold_date)

        result = check_source_staleness(
            "fred", dynamodb_mock, TEST_STATE_TABLE,
            "fxlake", threshold_days=2,
        )

        assert result is None

    def test_missing_dynamodb_entry_is_stale(self, dynamodb_mock):
        result = check_source_staleness(
            "frankfurter", dynamodb_mock, TEST_STATE_TABLE,
            "fxlake", threshold_days=2,
        )

        assert result is not None
        assert result.source == "frankfurter"
        assert result.last_date == "none"

    def test_correct_backfill_date_range(self, dynamodb_mock):
        stale_date = (TODAY - timedelta(days=10)).isoformat()
        _put_state(dynamodb_mock, "ecb", stale_date)

        result = check_source_staleness(
            "ecb", dynamodb_mock, TEST_STATE_TABLE,
            "fxlake", threshold_days=2,
        )

        assert result is not None
        expected_start = (TODAY - timedelta(days=9)).isoformat()
        assert result.backfill_start == expected_start
        assert result.backfill_end == TODAY.isoformat()

    def test_all_three_sources_checked(self):
        assert set(SOURCES) == {"frankfurter", "ecb", "fred"}


# ---------------------------------------------------------------------------
# trigger_backfill_execution
# ---------------------------------------------------------------------------
class TestTriggerBackfillExecution:
    def test_starts_execution_with_backfill_mode(self):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new-backfill"}

        stale = StaleSource(
            source="frankfurter",
            last_date="2024-01-01",
            gap_days=5,
            backfill_start="2024-01-02",
            backfill_end="2024-01-06",
        )

        arn = trigger_backfill_execution(sfn, SFN_ARN, stale)

        assert arn == "arn:new-backfill"
        call_kwargs = sfn.start_execution.call_args.kwargs
        input_data = json.loads(call_kwargs["input"])
        assert input_data["mode"] == "backfill"
        assert input_data["start_date"] == "2024-01-02"
        assert input_data["end_date"] == "2024-01-06"

    def test_execution_name_includes_source(self):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:x"}

        stale = StaleSource(
            source="ecb",
            last_date="2024-06-01",
            gap_days=3,
            backfill_start="2024-06-02",
            backfill_end="2024-06-04",
        )

        trigger_backfill_execution(sfn, SFN_ARN, stale)

        name = sfn.start_execution.call_args.kwargs["name"]
        assert "ecb" in name
        assert name.startswith("backfill-")

    def test_propagates_client_error(self):
        from botocore.exceptions import ClientError

        sfn = MagicMock()
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "StateMachineDoesNotExist", "Message": "gone"}},
            "StartExecution",
        )
        stale = StaleSource(
            source="fred",
            last_date="2024-01-01",
            gap_days=5,
            backfill_start="2024-01-02",
            backfill_end="2024-01-06",
        )

        with pytest.raises(ClientError):
            trigger_backfill_execution(sfn, SFN_ARN, stale)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    @patch("lambda_stale_data_backfill.boto3")
    def test_triggers_backfill_for_stale_sources(self, mock_boto3, dynamodb_mock):
        stale_date = (TODAY - timedelta(days=5)).isoformat()
        _put_state(dynamodb_mock, "frankfurter", stale_date)
        _put_state(dynamodb_mock, "ecb", (TODAY - timedelta(days=1)).isoformat())
        _put_state(dynamodb_mock, "fred", (TODAY - timedelta(days=1)).isoformat())

        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:bf"}
        cw = MagicMock()
        ddb = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "cloudwatch": cw, "dynamodb": ddb,
        }[svc]

        context = MagicMock(aws_request_id="req-1")

        with patch(
            "lambda_stale_data_backfill.check_source_staleness"
        ) as mock_check:
            stale = StaleSource(
                source="frankfurter",
                last_date=stale_date,
                gap_days=4,
                backfill_start=(TODAY - timedelta(days=4)).isoformat(),
                backfill_end=TODAY.isoformat(),
            )
            mock_check.side_effect = [stale, None, None]

            result = lambda_handler({}, context)

        assert result["sources_checked"] == 3
        assert result["backfills_triggered"] == 1
        sfn.start_execution.assert_called_once()

    @patch("lambda_stale_data_backfill.boto3")
    def test_no_backfill_when_all_fresh(self, mock_boto3, dynamodb_mock):
        for source in SOURCES:
            _put_state(dynamodb_mock, source, (TODAY - timedelta(days=1)).isoformat())

        sfn = MagicMock()
        cw = MagicMock()
        ddb = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "cloudwatch": cw, "dynamodb": ddb,
        }[svc]

        context = MagicMock(aws_request_id="req-2")

        with patch(
            "lambda_stale_data_backfill.check_source_staleness",
            return_value=None,
        ):
            result = lambda_handler({}, context)

        assert result["backfills_triggered"] == 0
        sfn.start_execution.assert_not_called()

    @patch("lambda_stale_data_backfill.boto3")
    def test_continues_on_sfn_failure(self, mock_boto3):
        from botocore.exceptions import ClientError

        sfn = MagicMock()
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "StartExecution",
        )
        cw = MagicMock()
        ddb = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "cloudwatch": cw, "dynamodb": ddb,
        }[svc]

        context = MagicMock(aws_request_id="req-3")

        stale = StaleSource(
            source="frankfurter",
            last_date="2024-01-01",
            gap_days=5,
            backfill_start="2024-01-02",
            backfill_end="2024-01-06",
        )

        with patch(
            "lambda_stale_data_backfill.check_source_staleness",
            side_effect=[stale, stale, None],
        ):
            result = lambda_handler({}, context)

        assert result["errors"] == 2
        assert result["backfills_triggered"] == 0

    @patch("lambda_stale_data_backfill.boto3")
    def test_returns_status_summary(self, mock_boto3):
        sfn = MagicMock()
        cw = MagicMock()
        ddb = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "cloudwatch": cw, "dynamodb": ddb,
        }[svc]

        context = MagicMock(aws_request_id="req-4")

        with patch(
            "lambda_stale_data_backfill.check_source_staleness",
            return_value=None,
        ):
            result = lambda_handler({}, context)

        assert "status" in result
        assert "sources_checked" in result
        assert "backfills_triggered" in result
        assert "errors" in result

    @patch("lambda_stale_data_backfill.boto3")
    def test_publishes_stale_data_metric(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:bf"}
        cw = MagicMock()
        ddb = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "cloudwatch": cw, "dynamodb": ddb,
        }[svc]

        context = MagicMock(aws_request_id="req-5")

        stale = StaleSource(
            source="fred",
            last_date="2024-01-01",
            gap_days=10,
            backfill_start="2024-01-02",
            backfill_end="2024-01-11",
        )

        with patch(
            "lambda_stale_data_backfill.check_source_staleness",
            side_effect=[None, None, stale],
        ):
            lambda_handler({}, context)

        cw.put_metric_data.assert_called()
        all_metrics = []
        for call in cw.put_metric_data.call_args_list:
            all_metrics.extend(m["MetricName"] for m in call.kwargs["MetricData"])
        assert "StaleDataDetected" in all_metrics
        assert "BackfillTriggered" in all_metrics
