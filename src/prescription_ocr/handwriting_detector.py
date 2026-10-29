import re
from difflib import SequenceMatcher

from .schemas import norm

SIGNATURE_LABEL = re.compile(r'^(signed|signature|sign)\b')
MIN_NOTE_CHARS = 3
# How far right of a "Signed:" label (fraction of page width) a signature can reach.
SIGNATURE_REACH = .35
TITLES = {'dr', 'mr', 'mrs', 'ms', 'miss', 'prof', 'printed', 'by', 'prescriber', 'name', 'signed'}


def clinician_names(doc):
    """Name tokens of the printing/prescribing clinician, taken from printed text."""
    names = set()
    for line in doc.lines():
        tokens = norm(line.get('Text', '')).split()
        # Only words right next to the cue, so a LINE that also holds the patient name
        # or medication text does not turn those words into "signature" names.
        for i, t in enumerate(tokens):
            if t == 'dr':
                near = tokens[max(0, i - 2):i] + tokens[i + 1:i + 3]
            elif t == 'by' and i and tokens[i - 1] in ('printed', 'signed'):
                near = tokens[i + 1:i + 4]
            else:
                continue
            names.update(n for n in near if len(n) >= 3 and n not in TITLES and not n.isdigit()
                         and n != 'patient')
    return names


def handwritten_lines(doc, hand):
    """Handwritten words grouped by their Textract LINE; words outside any LINE stand alone."""
    hand_ids = {w['Id'] for w in hand}
    groups, seen = [], set()
    for line in doc.lines():
        words = [w for w in doc.child_words(line) if w.get('Id') in hand_ids]
        if words:
            groups.append((line, words))
            seen.update(w['Id'] for w in words)
    groups.extend((None, [w]) for w in hand if w['Id'] not in seen)
    return groups


def signature_word_ids(doc, hand):
    """Handwritten words that are a signature rather than a note.

    A handwritten line is a signature when it is a short scrawl beside a printed
    "Signed:" label, when it is a short line reading as the clinician's name
    ("Dr Igwe"), or when it is only a stray letter or two. The last two rules also
    catch a signature showing through from the next sheet. A real sentence written
    next to the signature ("Please dispense 7 days") is longer and stays a note.
    """
    labels = [l for l in doc.lines() if SIGNATURE_LABEL.match(norm(l.get('Text', '')))]
    names = clinician_names(doc)

    def near_label(w):
        b = doc.bbox(w)
        y = b.get('Top', 0) + b.get('Height', 0) / 2
        for label in labels:
            lb = doc.bbox(label)
            h = lb.get('Height', .01)
            right = lb.get('Left', 0) + lb.get('Width', 0)
            if (label.get('Page', 1) == w.get('Page', 1)
                    and lb.get('Left', 0) <= b.get('Left', 0) <= right + SIGNATURE_REACH
                    and lb.get('Top', 0) - 4 * h <= y <= lb.get('Top', 0) + 2 * h):
                return True
        return False

    def is_name(w):
        t = norm(w.get('Text', ''))
        return len(t) >= 3 and any(SequenceMatcher(None, t, n).ratio() >= .75 for n in names)

    def is_scrawl(words):
        # A signature's loops are read as one or two stray letters ("a", "Qi"); a written
        # note has at least a short word.
        return len(re.sub(r'[^a-z0-9]', '', ''.join(w.get('Text', '') for w in words).casefold())) < MIN_NOTE_CHARS

    found = set()
    for _, words in handwritten_lines(doc, hand):
        if (is_scrawl(words)
                or (len(words) <= 3 and any(is_name(w) for w in words))
                or (len(words) <= 2 and all(near_label(w) for w in words))):
            found.update(w['Id'] for w in words)
    return found


def detect_handwriting(doc, tables, cfg):
    words = [b for b in doc.blocks if b.get('BlockType') == 'WORD']
    hand = [b for b in words if b.get('TextType') == 'HANDWRITING']
    signature = signature_word_ids(doc, hand)
    table_regions = [(t.get('Page',1), doc.bbox(t)) for t in doc.tables]
    def inside(w, box):
        b = doc.bbox(w)
        x, y = b.get('Left', 0)+b.get('Width',0)/2, b.get('Top',0)+b.get('Height',0)/2
        return box.get('Left',0) <= x <= box.get('Left',0)+box.get('Width',0) and box.get('Top',0) <= y <= box.get('Top',0)+box.get('Height',0)
    # A note is handwriting anywhere on any page, outside the printed tables (writing in
    # a table cell is that cell's value, and strike-through marks sit on table rows).
    free = [w for w in hand if w['Id'] not in signature
            and not any(page == w.get('Page', 1) and inside(w, box) for page, box in table_regions)]
    free_ids = {w['Id'] for w in free}
    note_ids = []
    for line, line_hand in handwritten_lines(doc, free):
        # A printed line with one word Textract mistook for handwriting is not a note.
        total = len(doc.child_words(line)) if line else 1
        if len(line_hand) * 2 >= total:
            note_ids.extend(w['Id'] for w in line_hand if w['Id'] in free_ids)
    flagged_tables = []
    for table in doc.tables:
        grid = doc.table_grid(table)
        ids = {i for cell in grid.values() for i in cell.word_ids}
        table_words = [doc.index[i] for i in ids if i in doc.index]
        if not table_words:
            box = doc.bbox(table)
            table_words = [w for w in words if w.get('Page',1)==table.get('Page',1) and inside(w, box)]
        count = sum(w.get('TextType') == 'HANDWRITING' for w in table_words)
        ratio = count / len(table_words) if table_words else 0
        if count >= cfg.min_handwritten_table_words and ratio >= cfg.handwritten_table_ratio:
            flagged_tables.append({'table_id':table['Id'],'page':table.get('Page',1),
                                   'handwritten_words':count,'total_words':len(table_words),
                                   'ratio':round(ratio,3)})
    return {'handwritten_notes': len(note_ids) >= cfg.top_note_min_words,
            'note_word_ids': note_ids,
            'signature_word_ids': sorted(signature),
            'handwritten_tables': flagged_tables,
            'handwritten_words_total': len(hand), 'words_total': len(words),
            'handwriting_detection_limit':'Textract TextType may miss unrecognized pen marks.'}
