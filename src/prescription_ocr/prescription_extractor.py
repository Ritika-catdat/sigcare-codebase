import argparse
import hashlib
import json
import logging
from pathlib import PurePosixPath
import boto3
from .config import Config
from .textract_ocr import TextractOCR
from .document_parser import Document
from .patient_provider_extractor import extract_patient, extract_provider
from .medication_table_extractor import extract_tables
from .handwriting_detector import detect_handwriting
from .output_contract import build_output

LOG = logging.getLogger(__name__)


def extract(response, source, cfg=None):
    cfg = cfg or Config()
    doc = Document(response)
    provider = extract_provider(doc)
    patient = extract_patient(doc)
    data, tables, review = extract_tables(doc, cfg)
    handwriting = detect_handwriting(doc, tables, cfg)
    ignored_ids = {item['table_id'] for item in handwriting['handwritten_tables']}
    for kind, rows in data.items():
        data[kind] = [r for r in rows if r['_source']['table_id'] not in ignored_ids]
    if handwriting['handwritten_notes']:
        review.append('Handwritten notes present')
    if ignored_ids:
        review.append('Fully handwritten table(s) excluded: ' + ', '.join(sorted(ignored_ids)))
    if response.get('Warnings'):
        review.append('Textract warnings: '+repr(response['Warnings']))
    if not any(data.values()):
        review.append('No structured medication/allergy rows extracted')
    if not patient['name']['value'] and not patient['prisoner_number']['value']:
        review.append('Missing patient name and prisoner number')
    if not provider['name']['value']:
        review.append('Provider not identified')
    if ignored_ids:
        # Only ignore entire document when virtually all recognized OCR words are handwriting.
        word_blocks = [b for b in doc.blocks if b.get('BlockType')=='WORD']
        handwriting_words = sum(w.get('TextType')=='HANDWRITING' for w in word_blocks)
        mostly_handwritten = len(word_blocks)>=4 and handwriting_words/len(word_blocks)>=cfg.handwritten_table_ratio
    else:
        mostly_handwritten = False
    status = 'IGNORED_HANDWRITTEN' if mostly_handwritten and ignored_ids else ('REVIEW_REQUIRED' if review else 'EXTRACTED')
    return {
        'prescription_id': source['prescription_id'],
        'source': source,
        'provider': provider,
        'patient': patient,
        **data,
        'extraction_metadata': {
            'status': status,
            'pages_processed': response.get('Pages') or len(doc.pages),
            'textract_job_id': response.get('JobId'),
            'handwriting': handwriting,
            'tables': tables,
            'review_reasons': list(dict.fromkeys(review)),
            'model': 'AWS Textract TABLES+FORMS+LAYOUT; deterministic v1'
        }
    }


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode('utf-8')


def process_one(bucket, key, config=None, s3=None, ocr=None, version=None, original_pdf_location=None, prescription_number=None):
    config = config or Config()
    if not key.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff')):
        raise ValueError('Unsupported extension')
    s3 = s3 or boto3.client('s3', region_name=config.region)
    ocr = ocr or TextractOCR(config.region)
    output_bucket = config.output_bucket or bucket
    source_id = hashlib.sha256(f'{bucket}/{key}/{version or ""}'.encode()).hexdigest()[:16]
    filename = PurePosixPath(key).stem
    output_name = f'{filename}-{source_id}.json'
    response = ocr.analyze_s3(bucket, key, version, config.poll_seconds, config.timeout_seconds)
    # TODO: Read this from preprocessing metadata: S3 URI of the processed
    # original PDF containing all prescriptions, BEFORE it was split.
    # Example: original_pdf_location = manifest_entry['processed_pdf_s3_uri']

    # TODO: Read the prescription's sequence/index WITHIN that original PDF.
    # Example: prescription_number = manifest_entry['prescription_number']
    # Keep blank until available; source_id is only an internal technical ID.

    # TODO: Map the exact S3 URI of the split PDF holding this prescription.
    # Since `bucket` and `key` refer to the split prescription PDF here,
    # its location can safely be constructed from them (or overridden in source).
    prescription_location = f's3://{bucket}/{key}'
    source = {'prescription_id':source_id, 'bucket':bucket, 'key':key,
              'version':version, 'original_pdf_location':original_pdf_location,
              'prescription_number':prescription_number,
              'prescription_location':prescription_location}
    internal = extract(response, source, config)
    doc = Document(response)
    handwriting = internal['extraction_metadata']['handwriting']
    output_key = config.output_prefix.strip('/')+'/'+output_name
    result = build_output(internal, doc, handwriting, source, output_bucket, output_key)
    s3.put_object(Bucket=output_bucket, Key=output_key,
                  Body=json_bytes(result), ContentType='application/json', ServerSideEncryption='AES256')
    if result['flag_for_review']:
        s3.put_object(Bucket=output_bucket, Key=config.review_prefix.strip('/')+'/'+output_name,
                      Body=json_bytes(result), ContentType='application/json', ServerSideEncryption='AES256')
    if config.store_raw:
        s3.put_object(Bucket=output_bucket, Key=config.raw_prefix.strip('/')+'/'+output_name,
                      Body=json_bytes(response), ContentType='application/json', ServerSideEncryption='AES256')
    LOG.info('Extracted %s review=%s', key, result['flag_for_review'])
    return result


def main():
    parser = argparse.ArgumentParser(description='Run Textract extraction on existing S3 prescription objects')
    parser.add_argument('--bucket', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--key')
    group.add_argument('--prefix')
    parser.add_argument('--version', default=None)
    parser.add_argument('--original-pdf-location', default=None)
    parser.add_argument('--prescription-number', default=None)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    cfg = Config()
    s3 = boto3.client('s3', region_name=cfg.region)
    if args.key:
        process_one(args.bucket, args.key, cfg, s3=s3, version=args.version, original_pdf_location=args.original_pdf_location, prescription_number=args.prescription_number)
        return
    if args.original_pdf_location or args.prescription_number:
        parser.error('--original-pdf-location and --prescription-number are supported with --key only; for batch input use process_one() with per-file metadata')
    paginator = s3.get_paginator('list_objects_v2')
    count = 0
    for page in paginator.paginate(Bucket=args.bucket, Prefix=args.prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.lower().endswith(('.pdf','.png','.jpg','.jpeg','.tif','.tiff')):
                try:
                    process_one(args.bucket, key, cfg, s3=s3)
                except Exception:
                    LOG.exception('Failed to extract %s', key)
                count += 1
                if args.limit and count >= args.limit:
                    return

if __name__ == '__main__':
    main()
