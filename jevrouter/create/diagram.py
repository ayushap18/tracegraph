"""Native diagrams (docs/PLAN-accuracy-v2.md C4): timeline, tree and flow blocks laid out once in points, then drawn as
vector shapes in PDF, native shapes in PPTX, a PNG in DOCX and a mermaid block in Markdown (an XLSX gets a table).

A diagram never relies on colour: boxes are filled with the theme's stripe colour, outlined and labelled in its text
colour, and every fact is a text label. `clean_block` is the normalize step for these blocks (sizes, one root, no
cycles), and `layout` is the only place positions are computed, so every format draws the same picture.
"""
import io
import math
import re
from dataclasses import dataclass, field

KINDS = ('timeline', 'tree', 'flow')
MAX_EVENTS, MAX_TREE_NODES, MAX_FLOW_NODES, MAX_DEPTH = 30, 40, 12, 4
EVENT_LABEL, NODE_LABEL, EDGE_LABEL, DATE_LABEL = 80, 40, 40, 40

FONT, BOLD = 'Helvetica', 'Helvetica-Bold'
SIZE, LEAD, PAD = 8.5, 10.5, 4.0
WIDTH = 480.0          # the width a layout is computed at; backends scale it
MIN_LEAF = 72.0        # a layered tree needs this much width per leaf, else it is drawn as an indented list
MIN_COL = 110.0        # a left-to-right flow needs this much width per layer, else it runs top-down


@dataclass
class Layout:
    width: float = 0.0
    height: float = 0.0
    boxes: list[dict] = field(default_factory=list)   # {x, y, w, h, label, lines, bold}
    lines: list[dict] = field(default_factory=list)   # {x1, y1, x2, y2, arrow}
    labels: list[dict] = field(default_factory=list)  # {x, y, text, anchor, bold, size}: y is the baseline


# ---------- normalize ----------


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 3].rstrip() + '...'


def clean_block(b: dict, clean, note) -> dict | None:
    """One diagram block fixed for rendering, or None when too little is left. `clean(text) -> str` makes text plain;
    `note(rule, text)` records a fix."""
    kind = b['type']
    title = _short(clean(b.get('title') or ''), 120)
    if kind == 'timeline':
        events = []
        for e in b.get('events') or []:
            if not isinstance(e, dict):
                continue
            date, label = _short(clean(e.get('date') or ''), DATE_LABEL), clean(e.get('label') or '')
            if len(label) > EVENT_LABEL:
                label = _short(label, EVENT_LABEL)
                note('S6', f'timeline labels shortened to {EVENT_LABEL} characters')
            if date or label:
                events.append({'date': date, 'label': label})
        if len(events) > MAX_EVENTS:
            note('L2', f'timelines cut to {MAX_EVENTS} events')
            events = events[:MAX_EVENTS]
        if len(events) < 2:
            note('S6', 'a timeline with fewer than 2 events was left out')
            return None
        return {'type': kind, 'title': title or 'Timeline', 'events': events}
    raw = [n for n in b.get('nodes') or [] if isinstance(n, dict)]
    nodes, seen = [], set()
    for n in raw:
        nid = clean(n.get('id') if n.get('id') is not None else '')[:40] or clean(n.get('label') or '')[:40]
        label = clean(n.get('label') or '') or nid
        if not nid or nid in seen:
            continue
        if len(label) > NODE_LABEL:
            label = _short(label, NODE_LABEL)
            note('S6', f'diagram labels shortened to {NODE_LABEL} characters')
        seen.add(nid)
        nodes.append({'id': nid, 'label': label, 'parent': clean(n.get('parent') or '')[:40]})
    if kind == 'tree':
        return _clean_tree(title, nodes, note)
    return _clean_flow(title, nodes, b.get('edges') or [], clean, note)


def _clean_tree(title: str, nodes: list[dict], note) -> dict | None:
    ids = {n['id'] for n in nodes}
    roots = [n for n in nodes if not n['parent'] or n['parent'] not in ids or n['parent'] == n['id']]
    if not nodes:
        return None
    root = roots[0] if roots else nodes[0]
    for n in nodes:
        if n is root:
            n['parent'] = ''
        elif n in roots:
            n['parent'] = root['id']
            note('S6', 'a tree with several roots or unknown parents was joined under one root')
    parent = {n['id']: n['parent'] for n in nodes}
    for n in nodes:  # a parent chain that loops is cut at the root
        cur, steps = n['id'], 0
        while parent.get(cur) and steps <= len(nodes):
            cur, steps = parent[cur], steps + 1
        if steps > len(nodes):
            n['parent'] = parent[n['id']] = root['id']
            note('S6', 'a tree with a loop was fixed')

    def depth(nid):
        d = 0
        while parent.get(nid):
            nid, d = parent[nid], d + 1
        return d
    kept = [n for n in nodes if depth(n['id']) < MAX_DEPTH]
    if len(kept) < len(nodes):
        note('L2', f'trees cut to {MAX_DEPTH} levels')
    # breadth first, so a cut at 40 keeps the top of the tree
    kept.sort(key=lambda n: depth(n['id']))
    if len(kept) > MAX_TREE_NODES:
        note('L2', f'trees cut to {MAX_TREE_NODES} nodes')
        kept = kept[:MAX_TREE_NODES]
    order = {n['id']: i for i, n in enumerate(nodes)}
    kept.sort(key=lambda n: order[n['id']])
    if len(kept) < 2:
        note('S6', 'a tree with fewer than 2 nodes was left out')
        return None
    return {'type': 'tree', 'title': title or 'Structure',
            'nodes': [{'id': n['id'], 'parent': n['parent'], 'label': n['label']} for n in kept]}


def _clean_flow(title: str, nodes: list[dict], edges: list, clean, note) -> dict | None:
    if len(nodes) > MAX_FLOW_NODES:
        note('L2', f'flow diagrams cut to {MAX_FLOW_NODES} steps')
        nodes = nodes[:MAX_FLOW_NODES]
    ids = [n['id'] for n in nodes]
    by_label = {n['label'].lower(): n['id'] for n in nodes}
    out, seen = [], set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        a, b = clean(e.get('from') or ''), clean(e.get('to') or '')
        a, b = (a if a in ids else by_label.get(a.lower())), (b if b in ids else by_label.get(b.lower()))
        if not a or not b or a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        out.append({'from': a, 'to': b, 'label': _short(clean(e.get('label') or ''), EDGE_LABEL)})
    # drop back edges (depth-first in node order), so the flow has no cycle
    adj = {i: [] for i in ids}
    for e in out:
        adj[e['from']].append(e)
    state, back = {}, set()

    def visit(u):
        state[u] = 1
        for e in adj[u]:
            v = e['to']
            if state.get(v) == 1:
                back.add(id(e))
            elif not state.get(v):
                visit(v)
        state[u] = 2
    for i in ids:
        if not state.get(i):
            visit(i)
    if back:
        note('S6', 'a flow with a cycle had its back edges removed')
        out = [e for e in out if id(e) not in back]
    if len(nodes) < 2:
        note('S6', 'a flow with fewer than 2 steps was left out')
        return None
    return {'type': 'flow', 'title': title or 'Process', 'nodes': [{'id': n['id'], 'label': n['label']} for n in nodes],
            'edges': out}


def labels_of(block: dict) -> list[str]:
    """Every text label a diagram draws (dates, events, nodes, edges), for alt text and checks."""
    if block['type'] == 'timeline':
        return [x for e in block['events'] for x in (e['date'], e['label']) if x]
    out = [n['label'] for n in block.get('nodes') or []]
    return out + [e['label'] for e in block.get('edges') or [] if e.get('label')]


def alt_text(block: dict) -> str:
    return f'Diagram: {block.get("title") or block["type"]}. ' + '; '.join(labels_of(block))


def table_of(block: dict) -> tuple[list[str], list[list[str]]]:
    """The diagram as a table (Markdown fallback, XLSX)."""
    if block['type'] == 'timeline':
        return ['Date', 'Event'], [[e['date'], e['label']] for e in block['events']]
    labels = {n['id']: n['label'] for n in block['nodes']}
    if block['type'] == 'tree':
        return ['Item', 'Part of'], [[n['label'], labels.get(n['parent'], '')] for n in block['nodes']]
    if block['edges']:
        return ['From', 'To', 'Label'], [[labels[e['from']], labels[e['to']], e['label']] for e in block['edges']]
    return ['Step'], [[n['label']] for n in block['nodes']]


# ---------- layout ----------


def _width(text: str, font: str = FONT, size: float = SIZE) -> float:
    from reportlab.pdfbase import pdfmetrics
    try:
        return pdfmetrics.stringWidth(text, font, size)
    except Exception:
        return len(text) * size * 0.55


def wrap(text: str, width: float, font: str = FONT, size: float = SIZE) -> list[str]:
    """Words wrapped to `width` points; a word longer than the width is broken."""
    lines, cur = [], ''
    for word in str(text).split():
        while _width(word, font, size) > width and len(word) > 1:
            cut = max(1, int(len(word) * width / max(_width(word, font, size), 1)))
            while cut > 1 and _width(word[:cut], font, size) > width:
                cut -= 1
            if cur:
                lines.append(cur)
                cur = ''
            lines.append(word[:cut])
            word = word[cut:]
        trial = f'{cur} {word}' if cur else word
        if cur and _width(trial, font, size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur or not lines:
        lines.append(cur)
    return lines


def _box(x: float, y: float, w: float, label: str, bold: bool = False) -> dict:
    lines = wrap(label, w - 2 * PAD, BOLD if bold else FONT)
    return {'x': x, 'y': y, 'w': w, 'h': len(lines) * LEAD + 2 * PAD, 'label': label, 'lines': lines, 'bold': bold}


def _fit_box(x: float, y: float, max_w: float, label: str, bold: bool = False) -> dict:
    """A box as wide as its text needs, at most max_w."""
    need = _width(label, BOLD if bold else FONT) + 2 * PAD + 2
    return _box(x, y, min(max(need, 40.0), max_w), label, bold)


def layout(block: dict, width: float = WIDTH) -> Layout:
    kind = block['type']
    if kind == 'timeline':
        return _timeline(block, width)
    if kind == 'tree':
        return _tree(block, width)
    if kind == 'flow':
        return _flow(block, width)
    raise ValueError(f'not a diagram: {kind}')


def _timeline(block: dict, width: float) -> Layout:
    ev = block['events']
    out = Layout(width=width)
    if len(ev) <= 8:
        m = 8.0
        slot = (width - 2 * m) / len(ev)
        date_lines = [wrap(e['date'], slot - 6, BOLD) for e in ev]
        date_h = max(len(d) for d in date_lines) * LEAD
        axis = 4 + date_h + 8
        top = axis + 12
        tallest = 0.0
        for i, e in enumerate(ev):
            cx = m + slot * (i + 0.5)
            for j, ln in enumerate(date_lines[i]):
                out.labels.append({'x': cx, 'y': 4 + SIZE + j * LEAD, 'text': ln, 'anchor': 'middle', 'bold': True,
                                   'size': SIZE})
            out.lines.append({'x1': cx, 'y1': axis - 4, 'x2': cx, 'y2': top, 'arrow': False})
            if e['label']:
                b = _box(cx - (slot - 6) / 2, top, slot - 6, e['label'])
                out.boxes.append(b)
                tallest = max(tallest, b['h'])
        out.lines.insert(0, {'x1': m, 'y1': axis, 'x2': width - m, 'y2': axis, 'arrow': True})
        out.height = top + tallest + 4
        return out
    # a long timeline: a vertical axis, events alternating sides
    axis_x, y = width / 2, 6.0
    half = width / 2 - 26
    for i, e in enumerate(ev):
        left = i % 2 == 1
        b = _fit_box(0, y, half, e['label'] or e['date'])
        b['x'] = axis_x - 14 - b['w'] if left else axis_x + 14
        out.boxes.append(b)
        mid = y + b['h'] / 2
        out.lines.append({'x1': axis_x, 'y1': mid, 'x2': b['x'] + b['w'] if left else b['x'], 'y2': mid, 'arrow': False})
        date = wrap(e['date'], half - 8, BOLD)[0] if e['date'] and e['label'] else ''
        if date:
            out.labels.append({'x': axis_x + 8 if left else axis_x - 8, 'y': mid + SIZE / 3, 'text': date,
                               'anchor': 'start' if left else 'end', 'bold': True, 'size': SIZE})
        y += b['h'] + 6
    out.lines.insert(0, {'x1': axis_x, 'y1': 2, 'x2': axis_x, 'y2': y, 'arrow': True})
    out.height = y + 4
    return out


def _tree(block: dict, width: float) -> Layout:
    nodes = block['nodes']
    kids: dict[str, list[dict]] = {n['id']: [] for n in nodes}
    root = next(n for n in nodes if not n['parent'])
    for n in nodes:
        if n['parent'] in kids:
            kids[n['parent']].append(n)
    leaves: dict[str, int] = {}

    def count(n):
        leaves[n['id']] = sum(count(c) for c in kids[n['id']]) or 1
        return leaves[n['id']]
    count(root)
    out = Layout(width=width)
    if leaves[root['id']] * MIN_LEAF <= width:
        unit = width / leaves[root['id']]
        levels: dict[int, list[tuple[dict, float, float]]] = {}

        def place(n, x0, depth):
            span = leaves[n['id']] * unit
            levels.setdefault(depth, []).append((n, x0, span))
            x = x0
            for c in kids[n['id']]:
                place(c, x, depth + 1)
                x += leaves[c['id']] * unit
        place(root, 0.0, 0)
        y, where = 4.0, {}
        for depth in sorted(levels):
            row = []
            for n, x0, span in levels[depth]:
                w = min(span - 8, 140.0)
                b = _box(x0 + (span - w) / 2, y, w, n['label'], bold=depth == 0)
                row.append(b)
                where[n['id']] = b
            h = max(b['h'] for b in row)
            for b in row:  # one height per level, so the connectors line up
                b['y'] += (h - b['h']) / 2
            out.boxes += row
            y += h + 22
        for n in nodes:
            cs = kids[n['id']]
            if not cs or n['id'] not in where:
                continue
            p = where[n['id']]
            px, pb = p['x'] + p['w'] / 2, p['y'] + p['h']
            child_tops = [where[c['id']]['y'] for c in cs if c['id'] in where]
            mid = pb + (min(child_tops) - pb) / 2
            out.lines.append({'x1': px, 'y1': pb, 'x2': px, 'y2': mid, 'arrow': False})
            xs = [where[c['id']]['x'] + where[c['id']]['w'] / 2 for c in cs]
            if len(xs) > 1 or xs[0] != px:
                out.lines.append({'x1': min(xs + [px]), 'y1': mid, 'x2': max(xs + [px]), 'y2': mid, 'arrow': False})
            for c, cx in zip(cs, xs):
                out.lines.append({'x1': cx, 'y1': mid, 'x2': cx, 'y2': where[c['id']]['y'], 'arrow': False})
        out.height = y - 22 + 4
        return out
    # a wide tree: an indented list, one node per row, with elbow connectors
    y, where = 4.0, {}

    def row(n, depth):
        nonlocal y
        x = 6 + depth * 22
        b = _fit_box(x, y, min(width - x - 4, 300.0), n['label'], bold=depth == 0)
        out.boxes.append(b)
        where[n['id']] = b
        if n['parent']:
            p = where[n['parent']]
            px = p['x'] + 10
            out.lines.append({'x1': px, 'y1': p['y'] + p['h'], 'x2': px, 'y2': y + b['h'] / 2, 'arrow': False})
            out.lines.append({'x1': px, 'y1': y + b['h'] / 2, 'x2': x, 'y2': y + b['h'] / 2, 'arrow': False})
        y += b['h'] + 5
        for c in kids[n['id']]:
            row(c, depth + 1)
    row(root, 0)
    out.height = y
    return out


def _flow(block: dict, width: float) -> Layout:
    nodes, edges = block['nodes'], block['edges']
    ids = [n['id'] for n in nodes]
    incoming = {i: 0 for i in ids}
    for e in edges:
        incoming[e['to']] += 1
    layer, queue = {i: 0 for i in ids}, [i for i in ids if not incoming[i]]
    seen = set()
    while queue:
        u = queue.pop(0)
        seen.add(u)
        for e in edges:
            if e['from'] == u:
                layer[e['to']] = max(layer[e['to']], layer[u] + 1)
                incoming[e['to']] -= 1
                if incoming[e['to']] == 0:
                    queue.append(e['to'])
    layers: dict[int, list[dict]] = {}
    for n in nodes:
        layers.setdefault(layer[n['id']] if n['id'] in seen else 0, []).append(n)
    order = sorted(layers)
    out, where = Layout(width=width), {}
    lr = len(order) * MIN_COL <= width
    if lr:
        col = width / len(order)
        w = min(col - 30, 130.0)
        cols = []
        for k in order:
            boxes = [_box(0, 0, w, n['label']) for n in layers[k]]
            cols.append(boxes)
        tallest = max(sum(b['h'] for b in bs) + 14 * (len(bs) - 1) for bs in cols)
        for ci, (k, boxes) in enumerate(zip(order, cols)):
            total = sum(b['h'] for b in boxes) + 14 * (len(boxes) - 1)
            y = 6 + (tallest - total) / 2
            for n, b in zip(layers[k], boxes):
                b['x'], b['y'] = ci * col + (col - w) / 2, y
                where[n['id']] = b
                y += b['h'] + 14
                out.boxes.append(b)
        out.height = tallest + 12
    else:
        y = 6.0
        for k in order:
            row = layers[k]
            slot = width / len(row)
            w = min(slot - 16, 150.0)
            boxes = [_box(i * slot + (slot - w) / 2, y, w, n['label']) for i, n in enumerate(row)]
            h = max(b['h'] for b in boxes)
            for n, b in zip(row, boxes):
                b['y'] += (h - b['h']) / 2
                where[n['id']] = b
            out.boxes += boxes
            y += h + 26
        out.height = y - 26 + 6
    for e in edges:
        a, b = where[e['from']], where[e['to']]
        if lr:
            x1, y1, x2, y2 = a['x'] + a['w'], a['y'] + a['h'] / 2, b['x'], b['y'] + b['h'] / 2
        else:
            x1, y1, x2, y2 = a['x'] + a['w'] / 2, a['y'] + a['h'], b['x'] + b['w'] / 2, b['y']
        out.lines.append({'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2, 'arrow': True})
        if e['label']:
            out.labels.append({'x': (x1 + x2) / 2, 'y': (y1 + y2) / 2 - 2, 'text': e['label'], 'anchor': 'middle',
                               'bold': False, 'size': SIZE - 1.5, 'bg': True})
    return out


def arrow_head(x1, y1, x2, y2, size: float = 5.0) -> list[tuple[float, float]]:
    """The three corners of an arrow head at (x2, y2), pointing along the line."""
    ang = math.atan2(y2 - y1, x2 - x1)
    left = (x2 - size * math.cos(ang - 0.45), y2 - size * math.sin(ang - 0.45))
    right = (x2 - size * math.cos(ang + 0.45), y2 - size * math.sin(ang + 0.45))
    return [(x2, y2), left, right]


def overlaps(lay: Layout) -> list[tuple[int, int]]:
    """Pairs of boxes that overlap (a layout check for tests and the fit loop)."""
    out = []
    for i, a in enumerate(lay.boxes):
        for j in range(i + 1, len(lay.boxes)):
            b = lay.boxes[j]
            if a['x'] < b['x'] + b['w'] - 0.5 and b['x'] < a['x'] + a['w'] - 0.5 and \
                    a['y'] < b['y'] + b['h'] - 0.5 and b['y'] < a['y'] + a['h'] - 0.5:
                out.append((i, j))
    return out


# ---------- backends ----------


def pdf_drawing(block: dict, theme: dict, width: float, *, max_height: float | None = None, fit=None,
                font: str = FONT, bold: str = BOLD):
    """A reportlab Drawing of the diagram, `width` points wide (scaled down to max_height when taller). `fit(text) ->
    (text, font or None)` swaps in a font that can draw the text."""
    from reportlab.graphics.shapes import Drawing, Group, Line, Polygon, Rect, String
    from reportlab.lib import colors

    lay = layout(block, width)
    scale = min(1.0, (max_height / lay.height) if max_height and lay.height > max_height else 1.0)
    text_c, fill_c, bg_c = (colors.HexColor('#' + theme[k]) for k in ('text', 'stripe', 'bg'))
    H = lay.height
    g = Group()

    def face(text, is_bold):
        if fit is None:
            return text, bold if is_bold else font
        t, f = fit(text, is_bold)
        return t, f or (bold if is_bold else font)
    for ln in lay.lines:
        g.add(Line(ln['x1'], H - ln['y1'], ln['x2'], H - ln['y2'], strokeColor=text_c, strokeWidth=0.8))
        if ln['arrow']:
            pts = arrow_head(ln['x1'], H - ln['y1'], ln['x2'], H - ln['y2'])
            g.add(Polygon([v for p in pts for v in p], fillColor=text_c, strokeColor=text_c, strokeWidth=0.5))
    for b in lay.boxes:
        g.add(Rect(b['x'], H - b['y'] - b['h'], b['w'], b['h'], rx=3, ry=3, fillColor=fill_c, strokeColor=text_c,
                   strokeWidth=0.8))
        for i, line in enumerate(b['lines']):
            t, f = face(line, b['bold'])
            g.add(String(b['x'] + b['w'] / 2, H - (b['y'] + PAD + SIZE + i * LEAD - 1), t, fontName=f,
                         fontSize=SIZE, fillColor=text_c, textAnchor='middle'))
    for lab in lay.labels:
        t, f = face(lab['text'], lab['bold'])
        if lab.get('bg'):
            w = _width(t, f, lab['size']) + 4
            x0 = lab['x'] - w / 2 if lab['anchor'] == 'middle' else lab['x'] - (w if lab['anchor'] == 'end' else 0)
            g.add(Rect(x0, H - lab['y'] - 2, w, lab['size'] + 3, fillColor=bg_c, strokeColor=None))
        g.add(String(lab['x'], H - lab['y'], t, fontName=f, fontSize=lab['size'], fillColor=text_c,
                     textAnchor=lab['anchor']))
    if scale < 1:
        g.transform = (scale, 0, 0, scale, 0, 0)
    d = Drawing(width, H * scale)
    d.add(g)
    return d


def _pil_font(size: float, bold: bool = False):
    from PIL import ImageFont
    import os

    import reportlab
    base = os.path.join(os.path.dirname(reportlab.__file__), 'fonts')
    try:
        return ImageFont.truetype(os.path.join(base, 'VeraBd.ttf' if bold else 'Vera.ttf'), max(6, round(size)))
    except Exception:
        return ImageFont.load_default()


def png(block: dict, theme: dict, width_px: int) -> bytes:
    """The diagram as a PNG `width_px` wide (DOCX), drawn from the same layout. Text uses Bitstream Vera (shipped with
    reportlab), a little smaller than the layout's Helvetica so it stays inside the boxes."""
    from PIL import Image, ImageDraw

    lay = layout(block)
    s = width_px / lay.width
    W, H = width_px, max(1, int(math.ceil(lay.height * s)))

    def rgb(k):
        h = theme[k]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    text_c, fill_c, bg_c = rgb('text'), rgb('stripe'), rgb('bg')
    img = Image.new('RGB', (W, H), bg_c)
    d = ImageDraw.Draw(img)
    fonts = {False: _pil_font(SIZE * s * 0.9), True: _pil_font(SIZE * s * 0.9, True)}
    for ln in lay.lines:
        d.line([(ln['x1'] * s, ln['y1'] * s), (ln['x2'] * s, ln['y2'] * s)], fill=text_c, width=max(1, round(0.8 * s)))
        if ln['arrow']:
            d.polygon([(x * s, y * s) for x, y in arrow_head(ln['x1'], ln['y1'], ln['x2'], ln['y2'])], fill=text_c)
    for b in lay.boxes:
        d.rounded_rectangle([b['x'] * s, b['y'] * s, (b['x'] + b['w']) * s, (b['y'] + b['h']) * s], radius=3 * s,
                            fill=fill_c, outline=text_c, width=max(1, round(0.8 * s)))
        f = fonts[b['bold']]
        for i, line in enumerate(b['lines']):
            cx, base = (b['x'] + b['w'] / 2) * s, (b['y'] + PAD + SIZE + i * LEAD - 1) * s
            d.text((cx, base), line, font=f, fill=text_c, anchor='ms')
    for lab in lay.labels:
        f = _pil_font(lab['size'] * s * 0.9, lab['bold'])
        anchor = {'middle': 'ms', 'start': 'ls', 'end': 'rs'}[lab['anchor']]
        if lab.get('bg'):
            box = d.textbbox((lab['x'] * s, lab['y'] * s), lab['text'], font=f, anchor=anchor)
            d.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1], fill=bg_c)
        d.text((lab['x'] * s, lab['y'] * s), lab['text'], font=f, fill=text_c, anchor=anchor)
    buf = io.BytesIO()
    img.save(buf, 'PNG', dpi=(200, 200))
    return buf.getvalue()


def _mm(text: str) -> str:
    """Label text that can't break mermaid syntax."""
    s = re.sub(r'[\r\n`<>|"{}\[\]#;]', ' ', str(text))
    return re.sub(r'\s+', ' ', s).strip() or ' '


def mermaid(block: dict) -> str:
    kind = block['type']
    if kind == 'timeline':
        out = ['timeline', f'    title {_mm(block.get("title") or "Timeline").replace(":", " -")}']
        for e in block['events']:
            out.append(f'    {_mm(e["date"] or "-").replace(":", " -")} : {_mm(e["label"] or e["date"]).replace(":", " -")}')
        return '\n'.join(out)
    ref = {n['id']: f'n{i}' for i, n in enumerate(block['nodes'])}
    out = ['flowchart ' + ('TD' if kind == 'tree' else 'LR')]
    out += [f'    {ref[n["id"]]}["{_mm(n["label"])}"]' for n in block['nodes']]
    if kind == 'tree':
        out += [f'    {ref[n["parent"]]} --> {ref[n["id"]]}' for n in block['nodes'] if n['parent'] in ref]
    else:
        out += [f'    {ref[e["from"]]} -->' + (f'|{_mm(e["label"])}|' if e['label'] else '') + f' {ref[e["to"]]}'
                for e in block['edges']]
    return '\n'.join(out)


def pptx_draw(slide, block: dict, theme: dict, box, *, font: str | None = None) -> None:
    """Native shapes and connectors in a group named "Diagram: <title>", fitted into box (x, y, w, h in inches)."""
    from lxml import etree
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Pt

    x0, y0, bw, bh = box
    lay = layout(block)
    s = min(bw * 72 / lay.width, bh * 72 / lay.height)  # points of layout -> points on the slide
    ox = x0 * 72 + (bw * 72 - lay.width * s) / 2
    oy = y0 * 72

    def emu(pt):
        return Emu(int(pt * 12700))

    def rgb(k):
        return RGBColor(*(int(theme[k][i:i + 2], 16) for i in (0, 2, 4)))
    text_c, fill_c = rgb('text'), rgb('stripe')
    group = slide.shapes.add_group_shape()
    group.name = f'Diagram: {block.get("title") or block["type"]}'[:200]
    for ln in lay.lines:
        c = group.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, emu(ox + ln['x1'] * s), emu(oy + ln['y1'] * s),
                                       emu(ox + ln['x2'] * s), emu(oy + ln['y2'] * s))
        c.line.color.rgb, c.line.width = text_c, Pt(1)
        if ln['arrow']:
            tail = etree.SubElement(c.line._get_or_add_ln(), qn('a:tailEnd'))
            tail.set('type', 'triangle')
    size = max(8.0, SIZE * s)
    for b in lay.boxes:
        shp = group.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, emu(ox + b['x'] * s), emu(oy + b['y'] * s),
                                     emu(b['w'] * s), emu(b['h'] * s))
        shp.fill.solid()
        shp.fill.fore_color.rgb = fill_c
        shp.line.color.rgb, shp.line.width = text_c, Pt(1)
        shp.shadow.inherit = False
        tf = shp.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = emu(PAD * s * 0.5)
        tf.margin_top = tf.margin_bottom = emu(PAD * s * 0.3)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = b['label']
        r.font.size, r.font.color.rgb, r.font.bold = Pt(size * 0.92), text_c, b['bold'] or None
        if font:
            r.font.name = font
    for lab in lay.labels:
        w = _width(lab['text'], BOLD if lab['bold'] else FONT, lab['size']) * s + 12
        left = {'middle': lab['x'] * s - w / 2, 'start': lab['x'] * s - 3, 'end': lab['x'] * s - w + 3}[lab['anchor']]
        tb = group.shapes.add_textbox(emu(ox + left), emu(oy + (lab['y'] - lab['size']) * s - 3), emu(w),
                                      emu(lab['size'] * s + 8))
        tf = tb.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = {'middle': PP_ALIGN.CENTER, 'start': PP_ALIGN.LEFT, 'end': PP_ALIGN.RIGHT}[lab['anchor']]
        r = p.add_run()
        r.text = lab['text']
        r.font.size, r.font.color.rgb, r.font.bold = Pt(max(7.0, lab['size'] * s * 0.92)), text_c, lab['bold'] or None
        if font:
            r.font.name = font
