
def detect_handwriting(doc, tables, cfg):
    words = [b for b in doc.blocks if b.get('BlockType') == 'WORD']
    hand = [b for b in words if b.get('TextType') == 'HANDWRITING']
    firstpage = min(doc.pages, default=1)
    top = [w for w in hand if w.get('Page', 1) == firstpage and doc.bbox(w).get('Top', 1) <= .30]
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
            'handwritten_tables': flagged_tables,
            'handwritten_words_total': len(hand), 'words_total': len(words),
            'handwriting_detection_limit':'Textract TextType may miss unrecognized pen marks.'}
