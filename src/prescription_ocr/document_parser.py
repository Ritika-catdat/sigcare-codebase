from dataclasses import dataclass
from collections import defaultdict

@dataclass
class Cell:
    text: str
    page: int
    block_ids: list
    confidence: float | None
    bbox: dict | None
    word_ids: list
    row: int
    col: int
    is_header: bool = False

class Document:
    def __init__(self, response):
        self.blocks = response['Blocks']
        self.index = {b['Id']: b for b in self.blocks if 'Id' in b}
        self.pages = defaultdict(list)
        for b in self.blocks:
            self.pages[b.get('Page', 1)].append(b)
        self.tables = sorted([b for b in self.blocks if b.get('BlockType') == 'TABLE'],
                             key=lambda b: (b.get('Page', 1), self.bbox(b).get('Top', 0)))

    @staticmethod
    def bbox(block):
        return block.get('Geometry', {}).get('BoundingBox', {})

    @staticmethod
    def related(block, relationship):
        return [i for rel in block.get('Relationships', []) if rel.get('Type') == relationship
                for i in rel.get('Ids', [])]

    def child_words(self, block, visited=None):
        visited = visited or set()
        if block.get('Id') in visited:
            return []
        visited.add(block.get('Id'))
        if block.get('BlockType') == 'WORD':
            return [block]
        out = []
        for ident in self.related(block, 'CHILD'):
            if ident in self.index:
                out.extend(self.child_words(self.index[ident], visited))
        return out

    def text_of(self, block):
        if block.get('BlockType') in ('WORD', 'LINE'):
            return block.get('Text', '')
        if block.get('BlockType') == 'SELECTION_ELEMENT':
            return '[X]' if block.get('SelectionStatus') == 'SELECTED' else '[ ]'
        words = self.child_words(block)
        return ' '.join(w.get('Text', '') for w in words).strip()

    def provenance(self, block_ids, word_ids=None, page=None):
        ids = list(dict.fromkeys(block_ids or []))
        blocks = [self.index[i] for i in ids if i in self.index]
        if word_ids is None:
            words = {w['Id']: w for b in blocks for w in self.child_words(b)}
        else:
            words = {i: self.index[i] for i in word_ids if i in self.index}
        confs = [w['Confidence'] for w in words.values() if 'Confidence' in w]
        if not confs:
            confs = [b['Confidence'] for b in blocks if 'Confidence' in b]
        return {'page': page or (blocks[0].get('Page', 1) if blocks else None),
                'block_ids': ids, 'word_ids': list(words),
                'ocr_confidence': round(sum(confs) / len(confs), 2) if confs else None,
                'bounding_boxes': [{'block_id': b.get('Id'), 'box': self.bbox(b)} for b in blocks]}

    def lines(self, page=None):
        data = self.blocks if page is None else self.pages.get(page, [])
        return sorted([b for b in data if b.get('BlockType') == 'LINE'],
                      key=lambda b: (b.get('Page', 1), round(self.bbox(b).get('Top', 0), 3), self.bbox(b).get('Left', 0)))

    def form_pairs(self):
        found = []
        for b in self.blocks:
            if b.get('BlockType') != 'KEY_VALUE_SET' or 'KEY' not in b.get('EntityTypes', []):
                continue
            value_blocks = [self.index[i] for i in self.related(b, 'VALUE') if i in self.index]
            keytext = self.text_of(b)
            value = ' '.join(self.text_of(v) for v in value_blocks).strip()
            if keytext:
                found.append({'key': keytext, 'value': value, 'key_block': b,
                              'value_blocks': value_blocks, 'page': b.get('Page', 1)})
        return found

    def table_grid(self, table):
        cells = [self.index[i] for i in self.related(table, 'CHILD')
                 if i in self.index and self.index[i].get('BlockType') == 'CELL']
        merged = [self.index[i] for i in self.related(table, 'MERGED_CELL') if i in self.index]
        merges = {}
        for m in merged:
            members = set(self.related(m, 'CHILD'))
            for c in cells:
                if c.get('Id') in members:
                    merges[(c['RowIndex'], c['ColumnIndex'])] = m
        grid = {}
        for b in cells:
            row, col = b['RowIndex'], b['ColumnIndex']
            m = merges.get((row, col))
            # Only one anchor gets merged text; other positions remain blank.
            anchor = m and row == m.get('RowIndex', row) and col == m.get('ColumnIndex', col)
            shadow = m and not anchor
            source = m if anchor else b
            text = '' if shadow else self.text_of(source)
            words = [] if shadow else self.child_words(source)
            # Some MERGED_CELLs list child CELLS; text_of recursively handles those.
            grid[(row, col)] = Cell(text=text, page=b.get('Page', table.get('Page', 1)),
                                    block_ids=[source['Id']], confidence=source.get('Confidence'),
                                    bbox=self.bbox(source), word_ids=[w['Id'] for w in words],
                                    row=row, col=col,
                                    is_header='COLUMN_HEADER' in b.get('EntityTypes', []))
        return grid

    def adjacent_lines(self, table, before=0.1):
        tb = self.bbox(table)
        top = tb.get('Top', 0)
        return [l for l in self.lines(table.get('Page', 1)) if 0 <= top - self.bbox(l).get('Top', 0) <= before]
