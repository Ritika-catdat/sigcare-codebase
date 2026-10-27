from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Config:
    region: str = os.getenv('AWS_REGION', 'eu-west-2')
    input_bucket: str = os.getenv('INPUT_BUCKET', '')
    input_prefix: str = os.getenv('INPUT_PREFIX', 'preprocessed/')
    output_bucket: str = os.getenv('OUTPUT_BUCKET', '')
    output_prefix: str = os.getenv('OUTPUT_PREFIX', 'extracted/')
    review_prefix: str = os.getenv('REVIEW_PREFIX', 'review/')
    raw_prefix: str = os.getenv('RAW_PREFIX', 'raw-textract/')
    store_raw: bool = os.getenv('STORE_RAW', 'false').lower() == 'true'
    poll_seconds: int = int(os.getenv('POLL_SECONDS', '5'))
    timeout_seconds: int = int(os.getenv('TIMEOUT_SECONDS', '900'))
    min_cell_confidence: float = float(os.getenv('MIN_CELL_CONFIDENCE', '80'))
    min_table_header_score: float = float(os.getenv('MIN_TABLE_HEADER_SCORE', '0.45'))
    min_handwritten_table_words: int = int(os.getenv('MIN_HANDWRITTEN_TABLE_WORDS', '4'))
    handwritten_table_ratio: float = float(os.getenv('HANDWRITTEN_TABLE_RATIO', '0.75'))
    top_note_min_words: int = int(os.getenv('TOP_NOTE_MIN_WORDS', '2'))
