import re
from difflib import SequenceMatcher

from .schemas import norm

SIGNATURE_LABEL = re.compile(r'^(signed|signature|sign)\b')
MIN_NOTE_CHARS = 3
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


def signature_word_ids(doc, hand):
    """Handwritten words that are a signature rather than a note.

    A word is a signature when it sits on/just above a printed "Signed:" label, when
    its line reads as the clinician's name ("Dr Igwe"), or when its whole line is only
    a stray letter or two. The last two rules also catch a signature showing through
    from the next sheet onto the top of page 1.
    """
    labels = [l for l in doc.lines() if SIGNATURE_LABEL.match(norm(l.get('Text', '')))]
    names = clinician_names(doc)

    def near_label(w):
        b = doc.bbox(w)
        y = b.get('Top', 0) + b.get('Height', 0) / 2
        for label in labels:
            lb = doc.bbox(label)
            h = lb.get('Height', .01)
            if (label.get('Page', 1) == w.get('Page', 1) and b.get('Left', 0) >= lb.get('Left', 0)
                    and lb.get('Top', 0) - 4 * h <= y <= lb.get('Top', 0) + 2 * h):
                return True
        return False

    def is_name(w):
        t = norm(w.get('Text', ''))
        return len(t) >= 3 and any(SequenceMatcher(None, t, n).ratio() >= .75 for n in names)

    def is_scrawl(words):
        # A signature's loops are read as one or two stray letters ("a", "Qi"); a written
        # note has at least a short word. Signatures showing through from the next sheet
        # look like this, with no readable "Signed:" label beside them.
        return len(re.sub(r'[^a-z0-9]', '', ''.join(w.get('Text', '') for w in words).casefold())) < MIN_NOTE_CHARS

    hand_ids = {w['Id'] for w in hand}
    found = {w['Id'] for w in hand if near_label(w)}
    in_line = set()
    # One name-like word marks its whole handwritten line ("Dr Igwe", a scrawled initial).
    for line in doc.lines():
        words = [w for w in doc.child_words(line) if w.get('Id') in hand_ids]
        in_line.update(w['Id'] for w in words)
        if words and (any(is_name(w) for w in words) or is_scrawl(words)):
            found.update(w['Id'] for w in words)
    found.update(w['Id'] for w in hand if is_name(w) or (w['Id'] not in in_line and is_scrawl([w])))
    return found


def detect_handwriting(doc, tables, cfg):
    words = [b for b in doc.blocks if b.get('BlockType') == 'WORD']
    hand = [b for b in words if b.get('TextType') == 'HANDWRITING']
    signature = signature_word_ids(doc, hand)
    firstpage = min(doc.pages, default=1)
    top = [w for w in hand if w.get('Page', 1) == firstpage and doc.bbox(w).get('Top', 1) <= .30
           and w['Id'] not in signature]
    # Top handwriting overlapping patient/header printed table is not automatically a freeform note.
    table_regions = [(t.get('Page',1), doc.bbox(t)) for t in doc.tables]
    def inside(w, box):
        b = doc.bbox(w)
        x, y = b.get('Left', 0)+b.get('Width',0)/2, b.get('Top',0)+b.get('Height',0)/2
        return box.get('Left',0) <= x <= box.get('Left',0)+box.get('Width',0) and box.get('Top',0) <= y <= box.get('Top',0)+box.get('Height',0)
    top_notes = [w for w in top if not any(page == firstpage and inside(w, box) for page,box in table_regions)]
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
    return {'handwritten_notes': len(top_notes) >= cfg.top_note_min_words,
            'top_handwriting_word_ids':[w['Id'] for w in top_notes],
            'signature_word_ids': sorted(signature),
            'handwritten_tables': flagged_tables,
            'handwritten_words_total': len(hand), 'words_total': len(words),
            'handwriting_detection_limit':'Textract TextType may miss unrecognized pen marks.'}
