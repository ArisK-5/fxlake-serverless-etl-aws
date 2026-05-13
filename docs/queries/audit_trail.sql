-- ============================================================================
-- FXLake Audit Trail Query
-- ============================================================================
-- Purpose: Answer "Who accessed what data, when, and from where?"
--
-- Prerequisites:
--   1. CloudTrail logging to S3 (terraform/security.tf)
--   2. CloudTrail S3 data events enabled for processed bucket
--   3. Create a CloudTrail-compatible Athena table:
--      https://docs.aws.amazon.com/athena/latest/ug/cloudtrail-logs.html
--
-- Usage:
--   Run in Athena console against the cloudtrail_logs database.
--   Adjust the date range and bucket filter as needed.
-- ============================================================================

-- 1. Recent S3 access events on the processed bucket (last 7 days)
SELECT
    eventtime,
    useridentity.principalid                      AS principal,
    useridentity.arn                              AS caller_arn,
    sourceipaddress,
    eventname,
    json_extract_scalar(requestparameters, '$.bucketName') AS bucket,
    json_extract_scalar(requestparameters, '$.key')        AS object_key,
    useragent,
    CASE
        WHEN errorcode IS NOT NULL THEN 'FAILED'
        ELSE 'SUCCESS'
    END                                           AS status,
    errorcode,
    errormessage
FROM cloudtrail_logs
WHERE eventsource = 's3.amazonaws.com'
    AND json_extract_scalar(requestparameters, '$.bucketName')
        LIKE '%processed%'
    AND from_iso8601_timestamp(eventtime)
        >= current_timestamp - INTERVAL '7' DAY
ORDER BY eventtime DESC
LIMIT 1000;


-- 2. Failed access attempts (permission denied, auth errors)
SELECT
    eventtime,
    useridentity.arn      AS caller_arn,
    sourceipaddress,
    eventname,
    errorcode,
    errormessage,
    json_extract_scalar(requestparameters, '$.bucketName') AS bucket,
    json_extract_scalar(requestparameters, '$.key')        AS object_key
FROM cloudtrail_logs
WHERE errorcode IN ('AccessDenied', 'Client.UnauthorizedAccess')
    AND from_iso8601_timestamp(eventtime)
        >= current_timestamp - INTERVAL '30' DAY
ORDER BY eventtime DESC
LIMIT 500;


-- 3. Pipeline execution audit (Step Functions start/stop events)
SELECT
    eventtime,
    useridentity.arn  AS caller_arn,
    sourceipaddress,
    eventname,
    json_extract_scalar(requestparameters, '$.stateMachineArn') AS state_machine,
    json_extract_scalar(responseelements, '$.executionArn')     AS execution_arn
FROM cloudtrail_logs
WHERE eventsource = 'states.amazonaws.com'
    AND eventname IN ('StartExecution', 'StopExecution')
    AND from_iso8601_timestamp(eventtime)
        >= current_timestamp - INTERVAL '30' DAY
ORDER BY eventtime DESC
LIMIT 200;


-- 4. Data modification audit (writes to raw and processed buckets)
--    Note: CloudTrail S3 data events are enabled for the processed bucket only.
--    Raw bucket writes appear here only if management events capture them.
SELECT
    eventtime,
    useridentity.arn  AS caller_arn,
    sourceipaddress,
    eventname,
    json_extract_scalar(requestparameters, '$.bucketName') AS bucket,
    json_extract_scalar(requestparameters, '$.key')        AS object_key,
    json_extract_scalar(additionalEventData, '$.bytesTransferredIn') AS bytes_written
FROM cloudtrail_logs
WHERE eventsource = 's3.amazonaws.com'
    AND eventname IN ('PutObject', 'DeleteObject', 'CopyObject')
    AND (
        json_extract_scalar(requestparameters, '$.bucketName') LIKE '%raw%'
        OR json_extract_scalar(requestparameters, '$.bucketName') LIKE '%processed%'
    )
    AND from_iso8601_timestamp(eventtime)
        >= current_timestamp - INTERVAL '7' DAY
ORDER BY eventtime DESC
LIMIT 500;
