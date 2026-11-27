import math
import re
from dataclasses import dataclass
from collections import Counter, defaultdict

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
    # (box, text) per word in reading order, after overflowing words were given back to
    # their column; None when the words are the cell's own word_ids unchanged.
    pieces: list | None = None

class Document:
    def __init__(self, response):
        self.blocks = response['Blocks']
        self.index = {b['Id']: b for b in self.blocks if 'Id' in b}
        self.pages = defaultdict(list)
        for b in self.blocks:
            self.pages[b.get('Page', 1)].append(b)
        self.tables = sorted([b for b in self.blocks if b.get('BlockType') == 'TABLE'],
                             key=lambda b: (b.get('Page', 1), self.bbox(b).get('Top', 0)))
        self.tables = sorted(self.tables + self._layout_tables(),
                             key=lambda b: (b.get('Page', 1), self.reading_box(b).get('Top', 0)))

    @staticmethod
    def bbox(block):
        return block.get('Geometry', {}).get('BoundingBox', {})

    def page_rotation(self, page):
        """Clockwise turn of the text on a page (0, 90, 180 or 270) as Textract saw it.

        Textract starts each word's polygon at the text's own top-left corner, so on a page
        scanned upside down the first point is at the bottom-right of the box. Coordinates
        stay those of the scanned image, so on such a page the footer is at the "top".
        """
        cache = self.__dict__.setdefault('_rotation', {})
        if page not in cache:
            turns = []
            for w in self.pages.get(page, []):
                if w.get('BlockType') != 'WORD':
                    continue
                poly = w.get('Geometry', {}).get('Polygon') or []
                if len(poly) >= 2:
                    dx, dy = poly[1].get('X', 0) - poly[0].get('X', 0), poly[1].get('Y', 0) - poly[0].get('Y', 0)
                    if dx or dy:
                        turns.append(round(math.degrees(math.atan2(dy, dx)) / 90) % 4 * 90)
            cache[page] = Counter(turns).most_common(1)[0][0] if turns else 0
        return cache[page]

    def reading_box(self, block):
        """The block's box as it would be on the page turned the right way up."""
        b = self.bbox(block)
        left, top = b.get('Left', 0), b.get('Top', 0)
        width, height = b.get('Width', 0), b.get('Height', 0)
        rotation = self.page_rotation(block.get('Page', 1))
        if rotation == 180:
            return {'Left': 1 - left - width, 'Top': 1 - top - height, 'Width': width, 'Height': height}
        if rotation == 90:
            return {'Left': top, 'Top': 1 - left - width, 'Width': height, 'Height': width}
        if rotation == 270:
            return {'Left': 1 - top - height, 'Top': left, 'Width': height, 'Height': width}
        return b

    def table_word_ids(self):
        """Ids of every word inside a table cell."""
        if not hasattr(self, '_table_words'):
            self._table_words = {w['Id'] for t in self.tables for w in self.child_words(t)}
        return self._table_words

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
        words = self.in_reading_order(self.child_words(block))
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

    # ---- Tables Textract returned only as page layout ----

    # A line this close (page width) to the left of a column header still starts in it.
    LAYOUT_COLUMN_TOL = .006

    @staticmethod
    def _overlap(a, b):
        """Share of b's area that a covers (raw page coordinates)."""
        x = Document.bbox(a)
        y = Document.bbox(b)
        w = min(x.get('Left', 0) + x.get('Width', 0), y.get('Left', 0) + y.get('Width', 0)) - max(x.get('Left', 0), y.get('Left', 0))
        h = min(x.get('Top', 0) + x.get('Height', 0), y.get('Top', 0) + y.get('Height', 0)) - max(x.get('Top', 0), y.get('Top', 0))
        area = y.get('Width', 0) * y.get('Height', 0)
        return max(w, 0) * max(h, 0) / area if area else 0

    def _layout_tables(self):
        """Tables Textract marked only as a LAYOUT_TABLE, with no TABLE block of cells.

        On some scans (a darker tinted page, upside down) Textract finds the medicine table
        as page layout but returns no cells for it, so nothing was extracted. Such a table
        is rebuilt from the positions of its lines; it is used only when its first line
        reads like a table header and no real TABLE covers the same area.
        """
        from .schemas import TABLES, norm
        header_words = {w for schema in TABLES.values() for aliases in schema.values()
                        for alias in aliases for w in norm(alias).split()}
        found = []
        for block in self.blocks:
            if block.get('BlockType') != 'LAYOUT_TABLE':
                continue
            page = block.get('Page', 1)
            if any(t.get('Page', 1) == page and self._overlap(t, block) > .5 for t in self.tables):
                continue
            lines = [self.index[i] for i in self.related(block, 'CHILD')
                     if i in self.index and self.index[i].get('BlockType') == 'LINE']
            grid = self._grid_from_lines(lines, header_words, norm)
            if grid:
                found.append({**block, 'BlockType': 'TABLE', '_grid': grid})
        return found

    def _grid_from_lines(self, lines, header_words, norm):
        """Cells from line positions: columns start where the header lines start; a row
        starts at a printed line that fills three or more columns and begins the first
        column with a capital ("Dapagliflozi"), and the lines under it ("n 10mg",
        "tablets") belong to the same row until the next one."""
        if len(lines) < 4:
            return None
        boxes = {l['Id']: self.reading_box(l) for l in lines}
        heights = sorted(b.get('Height', 0) for b in boxes.values())
        tol = .6 * (heights[len(heights) // 2] or .01)
        clusters = []
        for line in sorted(lines, key=lambda l: boxes[l['Id']].get('Top', 0)):
            top = boxes[line['Id']].get('Top', 0)
            if clusters and top - clusters[-1][-1][0] < tol:
                clusters[-1].append((top, line))
            else:
                clusters.append([(top, line)])
        header = [l for _, l in clusters[0]]
        if sum(bool(set(norm(l.get('Text', '')).split()) & header_words) for l in header) < 3:
            return None
        lefts = sorted(boxes[l['Id']].get('Left', 0) for l in header)

        def column_at(box):
            left = box.get('Left', 0) + self.LAYOUT_COLUMN_TOL
            return max([i for i, x in enumerate(lefts) if x <= left], default=0)

        def column(line):
            return column_at(boxes[line['Id']])

        # Textract can read across a column line as one line ("tablets t": the end of
        # Drug name and the last letter of a Dose cut by character). Each line is split
        # into one piece per column its words fall in, so every word lands in its column.
        pieces = []
        for _, line in (item for cluster in clusters for item in cluster):
            by_column = defaultdict(list)
            for word in self.in_reading_order(self.child_words(line)):
                by_column[column_at(self.reading_box(word))].append(word)
            if len(by_column) < 2:
                pieces.append((line, line))
                continue
            for c, words in sorted(by_column.items()):
                word_boxes = [self.reading_box(w) for w in words]
                left = min(b.get('Left', 0) for b in word_boxes)
                top = min(b.get('Top', 0) for b in word_boxes)
                piece = {'Id': f"{line['Id']}#{c}", 'Page': line.get('Page', 1), '_line': line['Id'],
                         'Text': ' '.join(w.get('Text', '') for w in words), '_words': words}
                boxes[piece['Id']] = {
                    'Left': left, 'Top': top,
                    'Width': max(b.get('Left', 0) + b.get('Width', 0) for b in word_boxes) - left,
                    'Height': max(b.get('Top', 0) + b.get('Height', 0) for b in word_boxes) - top}
                pieces.append((line, piece))
        split = defaultdict(list)
        for line, piece in pieces:
            split[line['Id']].append(piece)
        clusters = [[(top, piece) for top, line in cluster for piece in split[line['Id']]] for cluster in clusters]

        rows = [[]]
        for k, cluster in enumerate(clusters):
            cl = [l for _, l in cluster]
            starts = (k > 0 and len({column(l) for l in cl}) >= 3
                      and any(column(l) == 0 and re.match(r'[A-Z]', l.get('Text', '')) for l in cl))
            if starts:
                rows.append([])
            rows[-1].extend(cl)
        if len(rows) < 2:
            return None
        right = max(b.get('Left', 0) + b.get('Width', 0) for b in boxes.values())
        edges = [x - self.LAYOUT_COLUMN_TOL for x in lefts] + [right]
        grid = {}
        for r, row_lines in enumerate(rows, 1):
            top = min(boxes[l['Id']].get('Top', 0) for l in row_lines)
            bottom = max(boxes[l['Id']].get('Top', 0) + boxes[l['Id']].get('Height', 0) for l in row_lines)
            by_col = defaultdict(list)
            for line in row_lines:
                by_col[column(line)].append(line)
            for c, cell_lines in by_col.items():
                cell_lines.sort(key=lambda l: (boxes[l['Id']].get('Top', 0), boxes[l['Id']].get('Left', 0)))
                box = {'Left': edges[c], 'Top': top, 'Width': max(edges[c + 1] - edges[c], 0), 'Height': bottom - top}
                words = [w for l in cell_lines for w in (l.get('_words') or self.child_words(l))]
                confs = [w['Confidence'] for w in words if 'Confidence' in w]
                grid[(r, c + 1)] = Cell(
                    text=self._join_lines([(boxes[l['Id']], l.get('Text', '')) for l in cell_lines], box),
                    page=cell_lines[0].get('Page', 1),
                    block_ids=list(dict.fromkeys(l.get('_line', l['Id']) for l in cell_lines)),
                    confidence=sum(confs) / len(confs) if confs else None, bbox=box,
                    word_ids=[w['Id'] for w in words], row=r, col=c + 1, is_header=r == 1)
        return grid

    def _reading_order(self, grid):
        """Renumber rows and columns in reading order.

        On some upside-down scans Textract numbers a table's rows and columns in scan
        order, so a label ("Prisoner number:") has its value in the column to its left
        and the rows run bottom-up. The grid is flipped so labels come before values.
        """
        def centre(cells, key, size):
            vals = [c.bbox.get(key, 0) + c.bbox.get(size, 0) / 2 for c in cells if c.bbox]
            return sum(vals) / len(vals) if vals else 0
        rows = sorted({r for r, _ in grid})
        cols = sorted({c for _, c in grid})
        flip_rows = len(rows) > 1 and (centre([v for (r, _), v in grid.items() if r == rows[0]], 'Top', 'Height')
                                       > centre([v for (r, _), v in grid.items() if r == rows[-1]], 'Top', 'Height'))
        flip_cols = len(cols) > 1 and (centre([v for (_, c), v in grid.items() if c == cols[0]], 'Left', 'Width')
                                       > centre([v for (_, c), v in grid.items() if c == cols[-1]], 'Left', 'Width'))
        if not flip_rows and not flip_cols:
            return grid
        out = {}
        for (r, c), cell in grid.items():
            nr = rows[0] + rows[-1] - r if flip_rows else r
            nc = cols[0] + cols[-1] - c if flip_cols else c
            cell.row, cell.col = nr, nc
            out[(nr, nc)] = cell
        return out

    def table_grid(self, table):
        if '_grid' in table:
            return dict(table['_grid'])
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
            # Textract can list a cell's lines out of order on a rotated scan ("10mg tablets"
            # before "Dapagliflozin"); words are taken in reading order on the upright page.
            words = [] if shadow else self.in_reading_order(self.child_words(source))
            # Boxes as on the upright page, so line breaks and cell edges are found the same
            # way on pages scanned sideways or upside down.
            text = '' if shadow else self._join_lines([(self.reading_box(w), w.get('Text', '')) for w in words],
                                                      self.reading_box(source)) or self.text_of(source)
            # Some MERGED_CELLs list child CELLS; text_of recursively handles those.
            grid[(row, col)] = Cell(text=text, page=b.get('Page', table.get('Page', 1)),
                                    block_ids=[source['Id']], confidence=source.get('Confidence'),
                                    bbox=self.reading_box(source), word_ids=[w['Id'] for w in words],
                                    row=row, col=col,
                                    is_header='COLUMN_HEADER' in b.get('EntityTypes', []))
        merged_positions = {k for k in grid if k in merges}
        flipped = self._reading_order(grid)
        if flipped is not grid:
            merged_positions = {(c.row, c.col) for k, c in grid.items() if k in merged_positions}
            grid = flipped
        self._split_overflowing_words(grid, merged_positions)
        return grid

    def in_reading_order(self, words):
        """Words top to bottom, left to right as on the upright page."""
        pieces = []
        for word in words:
            self._insert_in_reading_order(pieces, (self.reading_box(word), word))
        return [word for _, word in pieces]

    # Characters a column ruling line is read as when OCR runs a word across it.
    BORDER_CHARS = '|!iIl1'

    def _split_overflowing_words(self, grid, merged_positions):
        """Give back to the next column the part of a word that runs over a cell's right edge.

        In tightly packed tables OCR can read across a ruling line as one word, e.g.
        "tablet|times" -> "tabletitimes" in the Dose cell while "times" belongs to Script
        Notes. The word is cut where it crosses the edge (at the misread line character
        when there is one) and the overflow is inserted into the next cell in reading order.
        """
        pieces, word_of = {}, {}
        for key, cell in grid.items():
            if key not in merged_positions:
                pieces[key] = []
                for i in cell.word_ids:
                    if i in self.index:
                        box = self.reading_box(self.index[i])
                        word_of[id(box)] = i
                        pieces[key].append((box, self.index[i].get('Text', '')))
        changed = set()
        # A word that starts in the previous column ("28 tabletDo" put in Medication notes
        # where "28 tablet" belongs to Quantity): give the front of it back to that column.
        for (row, col), cell in sorted(grid.items()):
            prev = (row, col - 1)
            if (row, col) not in pieces or prev not in pieces or not cell.bbox:
                continue
            edge = cell.bbox.get('Left', 0)
            kept = []
            for box, text in pieces[(row, col)]:
                left, width = box.get('Left', 0), box.get('Width', 0)
                over = edge - left
                if 0 < width and over > max(.004, width / 2):
                    # Mostly in the previous column ("1 tablet, 1" with its last "1" read
                    # into Start date as "1 28 Oct 2026"): the whole word goes back.
                    self._insert_in_reading_order(pieces[prev], (box, text))
                    if word_of.get(id(box)) in cell.word_ids:
                        cell.word_ids = [i for i in cell.word_ids if i != word_of[id(box)]]
                        grid[prev].word_ids = grid[prev].word_ids + [word_of[id(box)]]
                    changed.update({prev, (row, col)})
                    continue
                short = len(text) < (2 if text.isdigit() else 4)
                if width <= 0 or short or left + width <= edge or over < max(.004, .2 * width):
                    kept.append((box, text))
                    continue
                head, tail = self._cut(text, round(len(text) * over / width))
                if head:
                    self._insert_in_reading_order(pieces[prev], ({**box, 'Width': over}, head))
                    changed.add(prev)
                if tail:
                    kept.append(({**box, 'Left': edge, 'Width': width - over}, tail))
                changed.add((row, col))
            pieces[(row, col)] = kept
        for (row, col), cell in sorted(grid.items()):
            nxt = (row, col + 1)
            if (row, col) not in pieces or nxt not in pieces or not cell.bbox:
                continue
            edge = cell.bbox.get('Left', 0) + cell.bbox.get('Width', 0)
            kept = []
            for box, text in pieces[(row, col)]:
                left, width = box.get('Left', 0), box.get('Width', 0)
                over = left + width - edge
                if 0 < width and over > width / 2:
                    # Mostly in the next column: the last letter of a narrow Dose cell cut by
                    # character ("1" / "table" / "t") that Textract put in Drug name
                    # ("Codeine 15mg tablets t"). The whole word goes to the next column.
                    self._insert_in_reading_order(pieces[nxt], (box, text))
                    if word_of.get(id(box)) in cell.word_ids:
                        cell.word_ids = [i for i in cell.word_ids if i != word_of[id(box)]]
                        grid[nxt].word_ids = grid[nxt].word_ids + [word_of[id(box)]]
                    changed.update({nxt, (row, col)})
                    continue
                if width <= 0 or len(text) < 4 or left >= edge or over < max(.004, .2 * width):
                    kept.append((box, text))
                    continue
                head, tail = self._cut(text, round(len(text) * (edge - left) / width))
                if head:
                    kept.append((box, head))
                if tail:
                    tail_box = {**box, 'Left': edge, 'Width': over}
                    self._insert_in_reading_order(pieces[nxt], (tail_box, tail))
                    changed.add(nxt)
                changed.add((row, col))
            pieces[(row, col)] = kept
        for key in changed:
            grid[key].text = self._join_lines(pieces[key], grid[key].bbox)
            grid[key].pieces = pieces[key]

    # A line ending closer than this (in character widths) to the cell's right edge is full.
    WRAP_EDGE_CHARS = 2
    # A line that ends with a strength ("50microgram", "2.5mg") ended at a word.
    STRENGTH_END = re.compile(r'\d(?:mg|g|ml|mls|microgram|micrograms|mcg|unit|units|%)$', re.IGNORECASE)
    # Words that start a new word, not finish one: "tablets" after "15mg", "Pharma" after
    # "(Aspire". A capitalised word of three letters or more is never the tail of a word.
    WORD_START = re.compile(
        r'^(?:[A-Z][a-z]{2,}|(?:tablets?|capsules?|cream|gel|ointment|solution|suspension|inhaler|spray'
        r'|drops|sachets?|patch(?:es)?|injection|lotion|shampoo|powder|liquid|syrup|modified|gastro'
        r'|release|oral|sugar|chewable|dispersible|effervescent|soluble|film|coated|ltd)\b)')

    # Whole words a line often ends with. When the next line starts with a word of two
    # letters or more, the line ended at this word ("times" / "per 1", "Ferrous" /
    # "fumarate"); a single letter after it is still the end of a cut word ("table" / "t").
    COMMON_WORDS = {
        'times', 'per', 'day', 'days', 'tablet', 'tablets', 'capsule', 'capsules', 'sachet', 'sachets',
        'ferrous', 'sodium', 'calcium', 'potassium', 'magnesium', 'oral', 'cream', 'gel', 'solution', 'the',
        'and', 'of', 'at', 'to', 'for', 'with', 'after', 'before', 'food', 'take', 'apply', 'one', 'two',
        'three', 'daily', 'morning', 'night', 'dose', 'each', 'every', 'week', 'weekly', 'hours', 'as',
        'required', 'needed', 'when', 'sugar', 'free', 'modified', 'release', 'gastro', 'resistant',
        'admin', 'pack', 'packs', 'in', 'possession', 'regular', 'dr', 'mr', 'mrs', 'ms', 'miss'}

    def _word_break(self, prev, text):
        """True when two pieces on consecutive lines are clearly separate words."""
        if prev.endswith('-'):
            return False  # "gastro-" / "resistant"
        last = re.findall(r'[A-Za-z]+$', prev)
        first = re.match(r'[A-Za-z]+', text)
        if last and first and last[0].lower() in self.COMMON_WORDS and len(first.group()) >= 2:
            return True
        return bool(self.STRENGTH_END.search(prev) or self.WORD_START.match(text))

    def _join_lines(self, pieces, cell_box):
        """Join a cell's words, closing up a word the printer broke across two lines.

        Narrow columns are wrapped by character, so "Aripiprazole" is printed as
        "Aripiprazol" at the end of one line and "e" at the start of the next. When a line
        runs to the cell's right edge and the next line carries on with a letter, the two
        pieces are one word. A word that OCR ran across the edge is not a wrap.
        """
        edge = (cell_box or {}).get('Left', 0) + (cell_box or {}).get('Width', 0)
        out = ''
        for i, (box, text) in enumerate(pieces):
            if i and text:
                prev_box, prev = pieces[i - 1]
                char = prev_box.get('Width', 0) / max(len(prev), 1)
                gap = edge - prev_box.get('Left', 0) - prev_box.get('Width', 0)
                new_line = box.get('Top', 0) >= prev_box.get('Top', 0) + .6 * prev_box.get('Height', 0)
                # A word ("Dapagliflozi" / "n") or a number ("202" / "6" in "15 Oct 2026").
                continues = ((re.search(r'[A-Za-z-]$', prev) and re.match(r'[A-Za-z]', text))
                             or (re.search(r'\d$', prev) and re.match(r'\d', text)))
                wrapped = (cell_box and new_line and -.5 * char <= gap < self.WRAP_EDGE_CHARS * char and continues
                           and not self._word_break(prev, text))
                # A hyphenated word broken after its hyphen: "gastro-" / "resistant".
                wrapped = wrapped or bool(new_line and re.search(r'[A-Za-z]-$', prev)
                                          and re.match(r'[a-z]', text))
                # A closing bracket on its own line closes the line above: "(08:00" / ")".
                wrapped = wrapped or bool(cell_box and new_line and re.fullmatch(r'[)\]]', text))
                if wrapped and re.fullmatch(r'[A-Z]', text) and re.search(r'[a-z]$', prev):
                    # The last letter of a lower-case word on its own line is often read as a
                    # capital: "Propranolo" / "I", "capsule" / "S".
                    text = 'l' if text == 'I' else text.lower()
                out += '' if wrapped else ' '
            out += text
        return out.strip()

    def _vocabulary(self):
        if not hasattr(self, '_vocab'):
            self._vocab = {re.sub(r'[^a-z0-9]', '', w.get('Text', '').casefold())
                           for w in self.blocks if w.get('BlockType') == 'WORD'}
        return self._vocab

    def _cut(self, text, at):
        """Split near the estimated edge position. Prefer a cut that drops a misread ruling
        line and leaves pieces that occur as words elsewhere in the same document."""
        vocab = self._vocabulary()
        def known(piece):
            key = re.sub(r'[^a-z0-9]', '', piece.casefold())
            return len(key) >= 2 and key in vocab
        best = None
        for i in range(max(1, at - 3), min(len(text) - 1, at + 3) + 1):
            options = [(text[:i], text[i:], 0)]
            if text[i] in self.BORDER_CHARS and i < len(text) - 1:
                options.append((text[:i], text[i + 1:], 1))
            for head, tail, border in options:
                score = 10 * (known(head) + known(tail)) + border - abs(i - at)
                if best is None or score > best[0]:
                    best = (score, head, tail)
        return (best[1], best[2]) if best else (text[:at], text[at:])

    @staticmethod
    def _insert_in_reading_order(pieces, piece):
        box = piece[0]
        middle = box.get('Top', 0) + box.get('Height', 0) / 2
        for i, (other, _) in enumerate(pieces):
            other_middle = other.get('Top', 0) + other.get('Height', 0) / 2
            same_line = abs(other_middle - middle) < max(box.get('Height', 0), other.get('Height', 0)) / 2
            if (same_line and other.get('Left', 0) > box.get('Left', 0)) or (not same_line and other_middle > middle):
                pieces.insert(i, piece)
                return
        pieces.append(piece)

    def adjacent_lines(self, table, before=0.1):
        """Lines just above the table on the upright page (its title, "IN Possession ...")."""
        top = self.reading_box(table).get('Top', 0)
        return [l for l in self.lines(table.get('Page', 1))
                if 0 <= top - self.reading_box(l).get('Top', 0) <= before]
