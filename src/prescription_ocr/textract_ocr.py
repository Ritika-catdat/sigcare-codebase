import time
import boto3
from botocore.exceptions import ClientError

class TextractOCR:
    def __init__(self, region, client=None):
        self.client = client or boto3.client('textract', region_name=region)

    def analyze_s3(self, bucket, key, version=None, poll_seconds=5, timeout_seconds=900):
        obj = {'Bucket': bucket, 'Name': key}
        if version:
            obj['Version'] = version
        result = self.client.start_document_analysis(
            DocumentLocation={'S3Object': obj},
            FeatureTypes=['TABLES', 'FORMS', 'LAYOUT'])
        job_id = result['JobId']
        start = time.monotonic()
        while True:
            if time.monotonic() - start > timeout_seconds:
                raise TimeoutError(f'Textract job {job_id} exceeded {timeout_seconds}s; job may still be running')
            try:
                response = self.client.get_document_analysis(JobId=job_id, MaxResults=1000)
            except ClientError as exc:
                if exc.response['Error']['Code'] in ('ThrottlingException', 'ProvisionedThroughputExceededException'):
                    time.sleep(poll_seconds)
                    continue
                raise
            status = response.get('JobStatus')
            if status in ('FAILED', 'PARTIAL_SUCCESS'):
                raise RuntimeError(f'Textract {status}: {response.get("StatusMessage", "")} {response.get("Warnings", [])}')
            if status == 'SUCCEEDED':
                break
            time.sleep(poll_seconds)
        blocks = list(response.get('Blocks', []))
        pages = response.get('DocumentMetadata', {}).get('Pages', 0)
        warnings = response.get('Warnings', [])
        token = response.get('NextToken')
        while token:
            paged = self.client.get_document_analysis(JobId=job_id, MaxResults=1000, NextToken=token)
            blocks.extend(paged.get('Blocks', []))
            token = paged.get('NextToken')
            warnings.extend(paged.get('Warnings', []))
        return {'JobId': job_id, 'Blocks': blocks, 'Pages': pages, 'Warnings': warnings}
