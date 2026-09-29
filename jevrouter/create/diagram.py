"""Native diagrams (docs/PLAN-accuracy-v2.md C4, docs/PLAN-designer.md 3.4): timeline, tree and flow blocks, and the
Studio kinds cycle, venn, pyramid, matrix, mindmap, process, comparison, labelled, stat-cards and scatter, laid out
once in points, then drawn as vector shapes in PDF, native (editable) shapes in PPTX, a PNG in DOCX and a mermaid block
in Markdown (an XLSX gets a table).

A diagram never relies on colour: every fact is a text label in the theme's text colour on the background or a light
tint, and colour (the theme's palette, which is grey in mono) only tells groups apart. `clean_block` is the normalize
step for these blocks (sizes, one root, no cycles, limits with a note), and `layout` is the only place positions are
computed, so every format draws the same picture. The theme dict decides the colours: a legacy theme
(create/themes.py) or `studio.tokens.legacy_theme(ds)`, so Studio's design tokens style the same drawing.

`KINDS` stays the three legacy kinds (tests and prompts enumerate it); `NEW_KINDS` are the Studio kinds
(`studio.plan.DIAGRAM_KINDS_NEW`) and `ALL_KINDS` is what the DocSpec accepts.
"""
import io
import math
import re
from dataclasses import dataclass, field

KINDS = ('timeline', 'tree', 'flow')
NEW_KINDS = ('cycle', 'venn', 'pyramid', 'matrix', 'mindmap', 'process', 'comparison', 'labelled', 'stat-cards',
             'scatter')
ALL_KINDS = KINDS + NEW_KINDS
MAX_EVENTS, MAX_TREE_NODES, MAX_FLOW_NODES, MAX_DEPTH = 30, 40, 12, 4
EVENT_LABEL, NODE_LABEL, EDGE_LABEL, DATE_LABEL = 80, 40, 40, 40
# the Studio kinds' limits (studio/plan.DIAGRAM_BLOCK_SCHEMAS): (fewest, most) of their main list
LABEL, DETAIL, VALUE = 60, 120, 24
LIMITS = {'cycle': (3, 8), 'venn': (2, 3), 'pyramid': (3, 6), 'matrix': (2, 4), 'mindmap': (2, 30), 'process': (2, 7),
          'comparison': (2, 3), 'labelled': (1, 8), 'stat-cards': (2, 4), 'scatter': (2, 200)}
GROUP_ITEMS = {'venn': 6, 'matrix': 6, 'comparison': 8}   # items per set, quadrant or column
QUADRANT_NAMES = ('Top left', 'Top right', 'Bottom left', 'Bottom right')
MINDMAP_DEPTH = 2          # levels below the centre idea
TITLES = {'timeline': 'Timeline', 'tree': 'Structure', 'flow': 'Process', 'cycle': 'Cycle', 'venn': 'Overlap',
          'pyramid': 'Pyramid', 'matrix': 'Matrix', 'mindmap': 'Mind map', 'process': 'Process',
          'comparison': 'Comparison', 'labelled': 'Labelled figure', 'stat-cards': 'Key figures',
          'scatter': 'Scatter plot'}

FONT, BOLD = 'Helvetica', 'Helvetica-Bold'
SIZE, LEAD, PAD = 8.5, 10.5, 4.0
WIDTH = 480.0          # the width a layout is computed at; backends scale it
MIN_LEAF = 72.0        # a layered tree needs this much width per leaf, else it is drawn as an indented list
MIN_COL = 110.0        # a left-to-right flow needs this much width per layer, else it runs top-down


@dataclass
class Layout:
    width: float = 0.0
    height: float = 0.0
    boxes: list[dict] = field(default_factory=list)   # {x, y, w, h, label, lines, bold} (+ the optional keys below)
    lines: list[dict] = field(default_factory=list)   # {x1, y1, x2, y2, arrow} (+ color, width, top)
    labels: list[dict] = field(default_factory=list)  # {x, y, text, anchor, bold, size}: y is the baseline (+ color)
    shapes: list[dict] = field(default_factory=list)  # Studio kinds: rect, ellipse, polygon, polyline, image, icon


# ---------- normalize ----------


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 3].rstrip() + '...'


_EVENT = [re.compile(r'^\s*(?P<date>[^:]{1,40}?)\s*:\s+(?P<label>\S.*)$', re.S),
          re.compile(r'^\s*(?P<date>.{1,40}?)\s+[-\u2013\u2014]\s+(?P<label>\S.*)$', re.S),
          re.compile(r'^\s*(?P<date>(?:c\.\s*)?\d{2,4}s?(?:[-/.]\d{1,2}){0,2}(?:\s*(?:BC|BCE|AD|CE))?)\s+(?P<label>\S.*)$',
                     re.S | re.I)]


def parse_event(s: str) -> dict:
    """A timeline event written as text: "1973: first call", "1973 - first call" or "1973 first call"."""
    s = str(s).strip()
    for rx in _EVENT:
        m = rx.match(s)
        if m:
            return {'date': m.group('date').strip(), 'label': m.group('label').strip()}
    return {'date': '', 'label': s}


def _node(n) -> dict | None:
    """A node as a dict; a node written as text is its own id and label."""
    if isinstance(n, dict):
        return n
    if isinstance(n, str) and n.strip():
        return {'id': n.strip()[:40], 'label': n.strip()}
    return None


def raw_labels(b: dict) -> list[str]:
    """The words of a diagram block before cleaning (its title first), for one with too little to draw."""
    out = [str(b.get('title') or '')]
    if b.get('type') in NEW_KINDS:
        out += _new_raw(b)
    elif b.get('type') == 'timeline':
        for e in b.get('events') or []:
            e = parse_event(e) if isinstance(e, str) else e if isinstance(e, dict) else {}
            date, label = str(e.get('date') or '').strip(), str(e.get('label') or '').strip()
            out.append(f'{date}: {label}' if date and label else date or label)
    else:
        out += [str(n.get('label') or n.get('id') or '') for n in map(_node, b.get('nodes') or []) if n]
    return [x for x in out if x.strip()]


def clean_block(b: dict, clean, note) -> dict | None:
    """One diagram block fixed for rendering, or None when too little is left. `clean(text) -> str` makes text plain;
    `note(rule, text)` records a fix. Events and nodes may be written as text."""
    kind = b['type']
    title = _short(clean(b.get('title') or ''), 120)
    if kind in NEW_KINDS:
        out = _CLEAN[kind](b, clean, note)
        if out is None:
            return None
        return {'type': kind, 'title': title or TITLES[kind], **out}
    if kind == 'timeline':
        events = []
        for e in b.get('events') or []:
            if isinstance(e, str):
                e = parse_event(e)
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
            note('S6', 'a timeline with fewer than 2 events was shown as a list')
            return None
        return {'type': kind, 'title': title or 'Timeline', 'events': events}
    raw = [n for n in map(_node, b.get('nodes') or []) if n is not None]
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


def _rooted(nodes: list[dict], note) -> dict:
    """One root (orphans and extra roots joined under the first) and no parent loops; returns the root."""
    ids = {n['id'] for n in nodes}
    roots = [n for n in nodes if not n['parent'] or n['parent'] not in ids or n['parent'] == n['id']]
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
    return root


def _clean_tree(title: str, nodes: list[dict], note) -> dict | None:
    if not nodes:
        return None
    _rooted(nodes, note)
    parent = {n['id']: n['parent'] for n in nodes}

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
        note('S6', 'a tree with fewer than 2 nodes was shown as a list')
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
        note('S6', 'a flow with fewer than 2 steps was shown as a list')
        return None
    return {'type': 'flow', 'title': title or 'Process', 'nodes': [{'id': n['id'], 'label': n['label']} for n in nodes],
            'edges': out}


# ---------- normalize: the Studio kinds ----------


def _str(v) -> str:
    """The text of a value as written: strings as they are, finite numbers as text, a dict by its label-like key."""
    if isinstance(v, bool) or v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v) if math.isfinite(v) else ''
    if isinstance(v, dict):
        for k in ('label', 'text', 'name', 'title', 'value', 'step'):
            if isinstance(v.get(k), (str, int, float)) and not isinstance(v.get(k), bool) and _str(v[k]).strip():
                return _str(v[k])
    return ''


def _lab(v, clean, note, n: int = LABEL) -> str:
    s = clean(_str(v)).strip()
    if len(s) > n:
        note('S6', f'diagram labels shortened to {n} characters')
        s = _short(s, n)
    return s


def _labels(v, clean, note, n: int = LABEL) -> list[str]:
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        v = [v]
    if not isinstance(v, list):
        return []
    return [x for x in (_lab(i, clean, note, n) for i in v) if x]


def _cap(items: list, most: int, note, what: str) -> list:
    if len(items) > most:
        note('L2', f'{what} cut to {most}')
        return items[:most]
    return items


def _few(kind: str, note) -> None:
    note('S6', f'a {TITLES[kind].lower()} with too few parts was shown as a list')


def _groups(v, clean, note, kind: str) -> list[dict]:
    """Sets, quadrants or columns: {label, items} each; a group given as text is a label with no items."""
    if not isinstance(v, list):
        return []
    most = GROUP_ITEMS[kind]
    out = []
    for g in v:
        if isinstance(g, dict):
            label = _lab(g.get('label') if g.get('label') is not None else _str(g), clean, note)
            items = _labels(g.get('items'), clean, note)
        else:
            label, items = _lab(g, clean, note), []
        if len(items) > most:
            note('L2', f'{TITLES[kind].lower()} groups cut to {most} items')
            items = items[:most]
        out.append({'label': label, 'items': items})
    return out


def _clean_cycle(b, clean, note):
    steps = _labels(b.get('steps'), clean, note)
    lo, hi = LIMITS['cycle']
    steps = _cap(steps, hi, note, 'cycles')
    if len(steps) < lo:
        return _few('cycle', note)
    return {'steps': steps}


def _clean_venn(b, clean, note):
    sets = [s for s in _groups(b.get('sets'), clean, note, 'venn') if s['label'] or s['items']]
    for i, s in enumerate(sets):
        s['label'] = s['label'] or f'Set {i + 1}'
    lo, hi = LIMITS['venn']
    sets = _cap(sets, hi, note, 'Venn diagrams')
    if len(sets) < lo:
        return _few('venn', note)
    shared = _labels(b.get('shared'), clean, note)
    if len(shared) > GROUP_ITEMS['venn']:
        note('L2', f'overlap groups cut to {GROUP_ITEMS["venn"]} items')
        shared = shared[:GROUP_ITEMS['venn']]
    return {'sets': sets, 'shared': shared}


def _clean_pyramid(b, clean, note):
    levels = _labels(b.get('levels'), clean, note)
    lo, hi = LIMITS['pyramid']
    levels = _cap(levels, hi, note, 'pyramids')
    if len(levels) < lo:
        return _few('pyramid', note)
    return {'levels': levels}


def _clean_matrix(b, clean, note):
    quads = [q for q in _groups(b.get('quadrants'), clean, note, 'matrix') if q['label'] or q['items']]
    quads = _cap(quads, 4, note, 'matrix quadrants')
    if len(quads) < LIMITS['matrix'][0]:
        return _few('matrix', note)
    if len(quads) < 4:
        note('S6', 'a matrix with fewer than 4 quadrants was filled with empty ones')
        quads += [{'label': '', 'items': []} for _ in range(4 - len(quads))]
    return {'x_axis': _lab(b.get('x_axis'), clean, note), 'y_axis': _lab(b.get('y_axis'), clean, note),
            'quadrants': quads}


def _clean_mindmap(b, clean, note):
    raw = [n for n in map(_node, b.get('nodes') or []) if n is not None]
    nodes, seen = [], set()
    for n in raw:
        nid = clean(n.get('id') if n.get('id') is not None else '')[:40] or clean(_str(n.get('label')))[:40]
        label = _lab(n.get('label'), clean, note) or nid
        if not nid or nid in seen:
            continue
        seen.add(nid)
        nodes.append({'id': nid, 'label': label, 'parent': clean(_str(n.get('parent')))[:40]})
    if len(nodes) < LIMITS['mindmap'][0]:
        return _few('mindmap', note)
    root = _rooted(nodes, note)
    parent = {n['id']: n['parent'] for n in nodes}

    def chain(nid):
        out = [nid]
        while parent.get(out[-1]):
            out.append(parent[out[-1]])
        return out   # the node, its parent, ..., the root
    deep = False
    for n in nodes:
        c = chain(n['id'])
        if len(c) - 1 > MINDMAP_DEPTH:
            n['parent'] = parent[n['id']] = c[-2]   # its branch: the ancestor just below the centre idea
            deep = True
    if deep:
        note('S6', f'mind maps show {MINDMAP_DEPTH + 1} levels; deeper ideas were joined to their branch')
    depth = {n['id']: len(chain(n['id'])) - 1 for n in nodes}
    order = {n['id']: i for i, n in enumerate(nodes)}
    kept = sorted(nodes, key=lambda n: (depth[n['id']], order[n['id']]))
    kept = _cap(kept, LIMITS['mindmap'][1], note, 'mind maps')
    kept.sort(key=lambda n: order[n['id']])
    if len(kept) < 2:
        return _few('mindmap', note)
    ids = {n['id'] for n in kept}
    return {'nodes': [{'id': n['id'], 'label': n['label'], 'parent': n['parent'] if n['parent'] in ids else None}
                      for n in kept if n['id'] == root['id'] or n['parent'] in ids]}


def _split_step(s: str) -> tuple[str, str]:
    m = re.match(r'^\s*([^:]{1,48}):\s+(\S.*)$', s, re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else (s, '')


def _clean_process(b, clean, note):
    steps = []
    for st in b.get('steps') if isinstance(b.get('steps'), list) else []:
        if isinstance(st, dict):
            label, detail = _str(st.get('label') if st.get('label') is not None else _str(st)), _str(st.get('detail'))
        else:
            label, detail = _split_step(_str(st))
        label, detail = _lab(label, clean, note), _lab(detail, clean, note, DETAIL)
        if label or detail:
            steps.append({'label': label or detail, 'detail': detail if label else ''})
    lo, hi = LIMITS['process']
    steps = _cap(steps, hi, note, 'process diagrams')
    if len(steps) < lo:
        return _few('process', note)
    return {'steps': steps}


def _clean_comparison(b, clean, note):
    cols = [c for c in _groups(b.get('columns'), clean, note, 'comparison') if c['label'] or c['items']]
    for i, c in enumerate(cols):
        c['label'] = c['label'] or f'Option {i + 1}'
    lo, hi = LIMITS['comparison']
    cols = _cap(cols, hi, note, 'comparisons')
    if len(cols) < lo:
        return _few('comparison', note)
    return {'columns': cols}


def _fraction(v) -> float | None:
    """A callout position: 0..1, a percentage ("40%") or 0..100."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        t = v.strip().rstrip('%')
        pct = v.strip().endswith('%')
        try:
            v = float(t) / (100 if pct else 1)
        except ValueError:
            return None
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if 1 < v <= 100:
        v = v / 100
    return round(min(1.0, max(0.0, float(v))), 4)


def asset_path(sha: str):
    """The asset cache file of an image id (create/assets.CACHE/<sha>.png)."""
    from . import assets
    return assets.CACHE / f'{sha}.png'


def _clean_labelled(b, clean, note):
    sha = str(b.get('image') or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or not asset_path(sha).is_file():
        note('S6', 'a labelled figure whose picture is not in the asset cache was shown as a list')
        return None
    calls, bad = [], False
    for c in b.get('callouts') if isinstance(b.get('callouts'), list) else []:
        if not isinstance(c, dict):
            bad = True
            continue
        label, x, y = _lab(c.get('label') if c.get('label') is not None else _str(c), clean, note), \
            _fraction(c.get('x')), _fraction(c.get('y'))
        if not label or x is None or y is None:
            bad = bad or bool(label)
            continue
        calls.append({'label': label, 'x': x, 'y': y})
    if bad:
        note('S6', 'callouts without a position on the picture were left out')
    lo, hi = LIMITS['labelled']
    calls = _cap(calls, hi, note, 'labelled figures')
    if len(calls) < lo:
        return _few('labelled', note)
    return {'image': sha, 'callouts': calls}


def _icon_name(v) -> str | None:
    name = str(v or '').strip().lower()
    if not name:
        return None
    try:
        from ..studio import icons
    except Exception:
        return None
    if icons.get(name) is not None:
        return name
    return icons.pick(name)


def _clean_stats(b, clean, note):
    stats = []
    for st in b.get('stats') if isinstance(b.get('stats'), list) else []:
        if isinstance(st, dict):
            value, label, icon = st.get('value'), _lab(st.get('label'), clean, note), _icon_name(st.get('icon'))
        else:
            value, label, icon = _split_stat(_str(st)) + (None,)
            label = _lab(label, clean, note)
        if isinstance(value, bool) or value is None or (isinstance(value, float) and not math.isfinite(value)):
            value = ''
        if not isinstance(value, (int, float)):
            value = _lab(value, clean, note, VALUE)
        if value == '' and not label:
            continue
        out = {'value': value, 'label': label}
        if icon:
            out['icon'] = icon
        stats.append(out)
    lo, hi = LIMITS['stat-cards']
    stats = _cap(stats, hi, note, 'stat cards')
    if len(stats) < lo:
        return _few('stat-cards', note)
    return {'stats': stats}


def _split_stat(s: str) -> tuple[str, str]:
    """"72% of students: walk to school" or "72% walk to school" -> ("72%", "walk to school")."""
    s = s.strip()
    m = re.match(r'^([^:]{1,24}):\s+(\S.*)$', s)
    if m and re.search(r'\d', m.group(1)):
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r'^(\S*\d\S*)\s+(\S.*)$', s)
    if m:
        return m.group(1), m.group(2)
    return '', s


def number(v) -> float | None:
    """A finite number from a number or a numeric string ("1,200", "12.5%"), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        t = v.strip().replace(',', '').rstrip('%').strip()
        try:
            f = float(t)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _clean_scatter(b, clean, note):
    pts, bad = [], False
    for p in b.get('points') if isinstance(b.get('points'), list) else []:
        if isinstance(p, dict):
            x, y, label = number(p.get('x')), number(p.get('y')), p.get('label')
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y, label = number(p[0]), number(p[1]), p[2] if len(p) > 2 else None
        else:
            x = y = label = None
        if x is None or y is None:
            bad = True
            continue
        pt = {'x': int(x) if x.is_integer() and abs(x) < 1e15 else x, 'y': int(y) if y.is_integer() and abs(y) < 1e15 else y}
        label = _lab(label, clean, note) if label is not None else ''
        if label:
            pt['label'] = label
        pts.append(pt)
    if bad:
        note('S5', 'points without two numbers were left out of a scatter plot')
    lo, hi = LIMITS['scatter']
    pts = _cap(pts, hi, note, 'scatter plots')
    if len(pts) < lo:
        return _few('scatter', note)
    return {'x_label': _lab(b.get('x_label'), clean, note), 'y_label': _lab(b.get('y_label'), clean, note),
            'points': pts}


_CLEAN = {'cycle': _clean_cycle, 'venn': _clean_venn, 'pyramid': _clean_pyramid, 'matrix': _clean_matrix,
          'mindmap': _clean_mindmap, 'process': _clean_process, 'comparison': _clean_comparison,
          'labelled': _clean_labelled, 'stat-cards': _clean_stats, 'scatter': _clean_scatter}


def _show(v) -> str:
    """A number as a person writes it (12, 3.5, 1,200), text as it is."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return f'{v:,}' if abs(v) >= 10000 else str(v)
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return _show(int(v))
        return f'{v:,.4g}' if abs(v) >= 10000 else f'{v:.4g}'
    return str(v or '')


def _new_raw(b: dict) -> list[str]:
    """The words of a Studio-kind block as written (before cleaning), for one with too little to draw."""
    out = []
    for key in ('steps', 'levels'):
        for s in b.get(key) if isinstance(b.get(key), list) else []:
            if isinstance(s, dict):
                out.append(': '.join(x for x in (_str(s.get('label')) or _str(s), _str(s.get('detail'))) if x.strip()))
            else:
                out.append(_str(s))
    for key in ('sets', 'quadrants', 'columns'):
        for g in b.get(key) if isinstance(b.get(key), list) else []:
            if isinstance(g, dict):
                items = g.get('items') if isinstance(g.get('items'), list) else []
                label = _str(g.get('label')) or ''
                words = ', '.join(_str(i) for i in items if _str(i).strip())
                out.append(f'{label}: {words}' if label and words else label or words)
            else:
                out.append(_str(g))
    shared = b.get('shared') if isinstance(b.get('shared'), list) else []
    if any(_str(s).strip() for s in shared):
        out.append('Shared: ' + ', '.join(_str(s) for s in shared if _str(s).strip()))
    for n in b.get('nodes') if isinstance(b.get('nodes'), list) else []:
        n = _node(n)
        if n:
            out.append(_str(n.get('label')) or _str(n.get('id')))
    for st in b.get('stats') if isinstance(b.get('stats'), list) else []:
        if isinstance(st, dict):
            out.append(' '.join(x for x in (_show(st.get('value')) if st.get('value') is not None else '',
                                            _str(st.get('label'))) if x.strip()))
        else:
            out.append(_str(st))
    for c in b.get('callouts') if isinstance(b.get('callouts'), list) else []:
        out.append(_str(c))
    for p in b.get('points') if isinstance(b.get('points'), list) else []:
        if isinstance(p, dict):
            coords = ', '.join(_show(p.get(k)) for k in ('x', 'y') if p.get(k) is not None)
            label = _str(p.get('label'))
            out.append(f'{label} ({coords})' if label and coords else label or coords)
        elif isinstance(p, (list, tuple)):
            out.append(' '.join(_str(x) for x in p))
    return out


def labels_of(block: dict) -> list[str]:
    """Every text label a diagram draws (dates, events, nodes, edges), for alt text and checks."""
    kind = block['type']
    if kind in ('cycle', 'pyramid'):
        return list(block['steps' if kind == 'cycle' else 'levels'])
    if kind in ('venn', 'matrix', 'comparison'):
        groups = block['sets' if kind == 'venn' else 'quadrants' if kind == 'matrix' else 'columns']
        axes = [block.get('x_axis') or '', block.get('y_axis') or ''] if kind == 'matrix' else []
        return [x for x in axes + [y for g in groups for y in (g['label'], *g['items'])] + list(block.get('shared') or [])
                if x]
    if kind == 'mindmap':
        return [n['label'] for n in block['nodes']]
    if kind == 'process':
        return [x for s in block['steps'] for x in (s['label'], s.get('detail') or '') if x]
    if kind == 'labelled':
        return [c['label'] for c in block['callouts']]
    if kind == 'stat-cards':
        return [x for s in block['stats'] for x in (_show(s['value']), s['label']) if x]
    if kind == 'scatter':
        return [x for x in (block.get('x_label') or '', block.get('y_label') or '',
                            *(p.get('label') or '' for p in block['points'])) if x]
    if kind == 'timeline':
        return [x for e in block['events'] for x in (e['date'], e['label']) if x]
    out = [n['label'] for n in block.get('nodes') or []]
    return out + [e['label'] for e in block.get('edges') or [] if e.get('label')]


def alt_text(block: dict) -> str:
    kind = block['type']
    if kind == 'scatter':
        pts = block['points']
        xs, ys = [p['x'] for p in pts], [p['y'] for p in pts]
        head = (f'Scatter plot of {block.get("y_label") or "y"} against {block.get("x_label") or "x"}, {len(pts)} points, '
                f'x from {_show(min(xs))} to {_show(max(xs))}, y from {_show(min(ys))} to {_show(max(ys))}')
        named = [f'{p["label"]} ({_show(p["x"])}, {_show(p["y"])})' for p in pts if p.get('label')]
        return f'Diagram: {block.get("title") or kind}. ' + '; '.join([head] + named)
    return f'Diagram: {block.get("title") or kind}. ' + '; '.join(labels_of(block))


def table_of(block: dict) -> tuple[list[str], list[list[str]]]:
    """The diagram as a table (Markdown fallback, XLSX). Each label is a cell of its own."""
    kind = block['type']
    if kind == 'cycle':
        st = block['steps']
        return ['Step', 'Leads to'], [[s, st[(i + 1) % len(st)]] for i, s in enumerate(st)]
    if kind == 'pyramid':
        return ['Level', 'Item'], [[i + 1, s] for i, s in enumerate(block['levels'])]
    if kind in ('venn', 'comparison'):
        groups = block['sets' if kind == 'venn' else 'columns']
        rows = [[g['label'], it] for g in groups for it in (g['items'] or [''])]
        rows += [['Shared', it] for it in block.get('shared') or []]
        return (['Set', 'Item'] if kind == 'venn' else ['Column', 'Item']), rows
    if kind == 'matrix':
        rows = [[name, q['label'], it] for name, q in zip(QUADRANT_NAMES, block['quadrants'])
                for it in (q['items'] or [''])]
        rows += [[axis, block[key], ''] for axis, key in (('Horizontal axis', 'x_axis'), ('Vertical axis', 'y_axis'))
                 if block.get(key)]
        return ['Quadrant', 'Label', 'Item'], rows
    if kind == 'mindmap':
        labels = {n['id']: n['label'] for n in block['nodes']}
        return ['Idea', 'Branch of'], [[n['label'], labels.get(n['parent'] or '', '')] for n in block['nodes']]
    if kind == 'process':
        return ['Step', 'Detail'], [[s['label'], s.get('detail') or ''] for s in block['steps']]
    if kind == 'labelled':
        return ['Label', 'Across', 'Down'], [[c['label'], f'{round(c["x"] * 100)}%', f'{round(c["y"] * 100)}%']
                                             for c in block['callouts']]
    if kind == 'stat-cards':
        return ['Value', 'Label'], [[s['value'], s['label']] for s in block['stats']]
    if kind == 'scatter':
        return [block.get('x_label') or 'x', block.get('y_label') or 'y', 'Label'], \
            [[p['x'], p['y'], p.get('label') or ''] for p in block['points']]
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
    if kind in _LAYOUTS:
        return _LAYOUTS[kind](block, width)
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


# ---------- layout: the Studio kinds ----------
#
# Boxes may carry more than the legacy {x, y, w, h, label, lines, bold}: `fill` and `stroke` (theme keys, "p<n>" for
# the n-th palette colour, or None for none), `alpha` (fill opacity), `shape` (rect | ellipse | chevron | pentagon),
# `size`/`lead` (text size and line step), `bolds` (bold per line), `paras` ([text, bold] paragraphs, which PPTX
# writes as paragraphs), `align` (center | left | right), `valign` (top | middle), `color` (text colour key), `pad` and
# `dx` (text offset). Layout.shapes holds what is not a text box: {kind: rect | ellipse | polygon | polyline | image |
# icon, ..., top: bool} (top ones are drawn over the boxes).

SMALL = 7.5


def _tbox(x: float, y: float, w: float, paras: list, *, size: float = SIZE, align: str = 'center',
          fill: str | None = None, stroke: str | None = None, alpha: float = 1.0, shape: str = 'rect',
          pad: float = PAD, min_h: float = 0.0, valign: str = 'top', color: str = 'text', wrap_w: float | None = None,
          dx: float = 0.0) -> dict:
    """A text box sized to its wrapped paragraphs ([text, bold] each)."""
    lead = round(size * LEAD / SIZE, 3)
    lines, bolds = [], []
    for text, bold in paras:
        ls = wrap(text, max(8.0, (wrap_w if wrap_w is not None else w - 2 * pad)), BOLD if bold else FONT, size)
        lines += ls
        bolds += [bool(bold)] * len(ls)
    h = max(min_h, len(lines) * lead + 2 * pad)
    return {'x': x, 'y': y, 'w': w, 'h': h, 'label': ' '.join(t for t, _ in paras if t), 'lines': lines, 'bold': False,
            'bolds': bolds, 'paras': [[t, bool(b)] for t, b in paras], 'size': size, 'lead': lead, 'align': align,
            'fill': fill, 'stroke': stroke, 'alpha': alpha, 'shape': shape, 'valign': valign, 'color': color,
            'pad': pad, 'dx': dx}


def _pal(i: int) -> str:
    return f'p{i % 6}'


def _bezier(p0, p1, p2, p3, n: int = 16) -> list[tuple[float, float]]:
    out = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _inside(p, b, grow: float = 0.0) -> bool:
    return b['x'] - grow <= p[0] <= b['x'] + b['w'] + grow and b['y'] - grow <= p[1] <= b['y'] + b['h'] + grow


def _cycle(block: dict, width: float) -> Layout:
    steps = block['steps']
    n = len(steps)
    out = Layout(width=width)
    bw = min(118.0, width * 0.26)
    rx = min((width - bw) / 2 - 4, 200.0)
    bw = max(56.0, min(bw, 2 * rx * math.sin(math.pi / n) - 12)) if n > 2 else bw
    rx = (width - bw) / 2 - 4
    boxes = [_tbox(0, 0, bw, [[s, False]], fill=_pal(i), alpha=0.2, stroke=_pal(i), valign='middle', min_h=30)
             for i, s in enumerate(steps)]
    tall = max(b['h'] for b in boxes)
    ry = max(rx * 0.55, tall * 1.2)
    for _ in range(12):
        cy = ry + tall / 2 + 6
        for i, b in enumerate(boxes):
            a = -math.pi / 2 + 2 * math.pi * i / n
            b['x'] = width / 2 + rx * math.cos(a) - bw / 2
            b['y'] = cy + ry * math.sin(a) - b['h'] / 2
        lay = Layout(width=width, boxes=boxes)
        if not overlaps(lay, gap=6):
            break
        ry *= 1.15
    out.boxes = boxes
    cx = width / 2
    for i in range(n):
        a0 = -math.pi / 2 + 2 * math.pi * i / n
        a1 = a0 + 2 * math.pi / n
        pts = [(cx + rx * math.cos(a0 + (a1 - a0) * k / 60), cy + ry * math.sin(a0 + (a1 - a0) * k / 60))
               for k in range(61)]
        run, best = [], []
        for p in pts:
            if _inside(p, boxes[i], 5) or _inside(p, boxes[(i + 1) % n], 5):
                if len(run) > len(best):
                    best = run
                run = []
            else:
                run.append(p)
        if len(run) > len(best):
            best = run
        if len(best) >= 2:
            out.shapes.append({'kind': 'polyline', 'points': best, 'stroke': 'accent', 'width': 1.4, 'arrow': True})
    icon = min(rx, ry) * 0.45
    if icon >= 18 and n >= 3:
        out.shapes.append({'kind': 'icon', 'name': 'refresh-cw', 'x': cx - icon / 2, 'y': cy - icon / 2, 'size': icon,
                           'stroke': 'muted'})
    out.height = cy + ry + tall / 2 + 6
    return out


def _venn(block: dict, width: float) -> Layout:
    sets, shared = block['sets'], block.get('shared') or []
    out = Layout(width=width)

    def paras(s):
        return [[s['label'], True]] + [['• ' + it, False] for it in s['items']]
    if len(sets) == 2:
        r = min(width * 0.27, 130.0)
        d = r * 1.1
        cy = r + 6
        centres = [(width / 2 - d / 2, cy), (width / 2 + d / 2, cy)]
        spots = [(centres[0][0] - 0.45 * r, cy, 0.72 * r), (centres[1][0] + 0.45 * r, cy, 0.72 * r)]
        mid = (width / 2, cy, 0.62 * (2 * r - d))
    else:
        r = min(width * 0.2, 105.0)
        d = r * 1.1
        top = r + 6
        centres = [(width / 2 - d / 2, top), (width / 2 + d / 2, top), (width / 2, top + d * 0.866)]
        spots = [(centres[0][0] - 0.42 * r, top - 0.3 * r, 0.7 * r), (centres[1][0] + 0.42 * r, top - 0.3 * r, 0.7 * r),
                 (centres[2][0], centres[2][1] + 0.42 * r, 0.9 * r)]
        mid = (width / 2, (2 * top + centres[2][1]) / 3, 0.42 * r)
    for i, (cx, cy) in enumerate(centres):
        out.shapes.append({'kind': 'ellipse', 'x': cx - r, 'y': cy - r, 'w': 2 * r, 'h': 2 * r, 'fill': _pal(i),
                           'alpha': 0.2, 'stroke': _pal(i), 'width': 1.2})
    for s, (x, y, w) in zip(sets, spots):
        b = _tbox(x - w / 2, 0, w, paras(s), size=SMALL, pad=2)
        b['y'] = y - b['h'] / 2
        out.boxes.append(b)
    if shared:
        x, y, w = mid
        b = _tbox(x - w / 2, 0, w, [['• ' + it if len(shared) > 1 else it, False] for it in shared], size=SMALL - 0.5,
                  pad=1)
        b['y'] = y - b['h'] / 2
        out.boxes.append(b)
    bottom = max([c[1] + r for c in centres] + [b['y'] + b['h'] for b in out.boxes])
    topmost = min([c[1] - r for c in centres] + [b['y'] for b in out.boxes])
    if topmost < 2:
        _shift(out, 0, 2 - topmost)
        bottom += 2 - topmost
    out.height = bottom + 6
    return out


def _shift(lay: Layout, dx: float, dy: float) -> None:
    for b in lay.boxes:
        b['x'] += dx
        b['y'] += dy
    for s in lay.shapes:
        if 'points' in s:
            s['points'] = [(x + dx, y + dy) for x, y in s['points']]
        else:
            s['x'] += dx
            s['y'] += dy
    for ln in lay.lines:
        ln['x1'] += dx
        ln['x2'] += dx
        ln['y1'] += dy
        ln['y2'] += dy
    for lab in lay.labels:
        lab['x'] += dx
        lab['y'] += dy


def _pyramid(block: dict, width: float) -> Layout:
    levels = block['levels']
    n = len(levels)
    out = Layout(width=width)
    base = min(width * 0.8, 380.0)
    top_w = base * 0.3
    cx = width / 2
    heights = [34.0] * n
    for _ in range(2):   # widths depend on heights and heights on the wrapped text: two passes settle it
        total = sum(heights) + 3 * (n - 1)
        y, boxes = 4.0, []
        for i, lvl in enumerate(levels):
            w_top = top_w + (base - top_w) * (y - 4) / total
            b = _tbox(cx - (w_top - 18) / 2, y, w_top - 18, [[lvl, i == 0]], valign='middle', min_h=30, pad=5)
            boxes.append(b)
            y += heights[i] + 3
        heights = [max(h, b['h']) for h, b in zip(heights, boxes)]
    total = sum(heights) + 3 * (n - 1)
    y = 4.0
    for i, (lvl, h) in enumerate(zip(levels, heights)):
        wt = top_w + (base - top_w) * (y - 4) / total
        wb = top_w + (base - top_w) * (y + h - 4) / total
        out.shapes.append({'kind': 'polygon', 'points': [(cx - wt / 2, y), (cx + wt / 2, y), (cx + wb / 2, y + h),
                                                         (cx - wb / 2, y + h)],
                           'fill': _pal(i), 'alpha': 0.24, 'stroke': _pal(i), 'width': 1.0})
        b = _tbox(cx - (wt - 18) / 2, y, wt - 18, [[lvl, i == 0]], valign='middle', min_h=h, pad=5)
        b['h'] = h
        out.boxes.append(b)
        y += h + 3
    out.height = y + 1
    return out


def _matrix(block: dict, width: float) -> Layout:
    q = block['quadrants']
    xa, ya = block.get('x_axis') or '', block.get('y_axis') or ''
    axes = bool(xa or ya)
    out = Layout(width=width)
    ml = 16.0 if axes else 4.0
    y0 = 18.0 if ya else 4.0
    gap = 6.0
    cw = (width - ml - 6 - gap) / 2
    boxes = []
    for i, quad in enumerate(q):
        paras = ([[quad['label'], True]] if quad['label'] else []) + [['• ' + it, False] for it in quad['items']]
        boxes.append(_tbox(0, 0, cw, paras or [['', False]], align='left', fill=_pal(i), alpha=0.16, stroke=_pal(i),
                           min_h=56, pad=6))
    rows = [max(boxes[0]['h'], boxes[1]['h']), max(boxes[2]['h'], boxes[3]['h'])]
    for i, b in enumerate(boxes):
        r, c = divmod(i, 2)
        b['x'] = ml + 4 + c * (cw + gap)
        b['y'] = y0 + r * (rows[0] + gap)
        b['h'] = rows[r]
    out.boxes = boxes
    bottom = y0 + rows[0] + gap + rows[1]
    if axes:
        out.lines.append({'x1': ml, 'y1': bottom + 4, 'x2': ml, 'y2': y0 - 8 if ya else y0, 'arrow': True,
                          'color': 'muted'})
        out.lines.append({'x1': ml, 'y1': bottom + 4, 'x2': width - 2, 'y2': bottom + 4, 'arrow': True,
                          'color': 'muted'})
        if ya:
            out.labels.append({'x': ml + 6, 'y': y0 - 7, 'text': ya, 'anchor': 'start', 'bold': True, 'size': SMALL})
        if xa:
            out.labels.append({'x': width - 4, 'y': bottom + 16, 'text': xa, 'anchor': 'end', 'bold': True,
                               'size': SMALL})
        out.height = bottom + (22 if xa else 10)
    else:
        out.height = bottom + 4
    return out


def _mindmap(block: dict, width: float) -> Layout:
    nodes = block['nodes']
    kids: dict[str, list[dict]] = {n['id']: [] for n in nodes}
    root = next(n for n in nodes if not n.get('parent'))
    for n in nodes:
        if n.get('parent') in kids:
            kids[n['parent']].append(n)
    branches = kids[root['id']]
    out = Layout(width=width)
    deep = any(kids[b['id']] for b in branches)
    rw = 84.0 if deep else 104.0
    gap = 14.0
    w1 = 84.0 if deep else min(150.0, width / 2 - rw / 2 - gap - 4)
    w2 = width / 2 - rw / 2 - gap - w1 - gap - 4
    right = branches[:(len(branches) + 1) // 2]
    left = branches[(len(branches) + 1) // 2:]
    rbox = _tbox(width / 2 - rw / 2, 0, rw, [[root['label'], True]], fill='header_bg', stroke=None, color='header_text',
                 valign='middle', min_h=34, pad=6)
    placed = []   # (branch index, L1 box, [L2 boxes], side)

    def side(branch_list, sign, first):
        blocks, y = [], 0.0
        for k, br in enumerate(branch_list):
            i = first + k
            b1 = _tbox(0, 0, w1, [[br['label'], True]], fill=_pal(i), alpha=0.22, stroke=_pal(i), valign='middle',
                       min_h=24, pad=4)
            l2 = [_tbox(0, 0, w2, [[c['label'], False]], size=SMALL, stroke=_pal(i), valign='middle', min_h=18, pad=3)
                  for c in kids[br['id']]]
            h2 = sum(b['h'] for b in l2) + 5 * max(0, len(l2) - 1)
            hb = max(b1['h'], h2)
            b1['y'] = y + (hb - b1['h']) / 2
            yy = y + (hb - h2) / 2
            for b in l2:
                b['y'] = yy
                yy += b['h'] + 5
            x1 = width / 2 + sign * (rw / 2 + gap) - (w1 if sign < 0 else 0)
            b1['x'] = x1
            for b in l2:
                b['x'] = x1 + w1 + gap if sign > 0 else x1 - gap - w2
            blocks.append((i, b1, l2, sign))
            y += hb + 10
        return blocks, max(0.0, y - 10)
    rb, rh = side(right, 1, 0)
    lb, lh = side(left, -1, len(right))
    H = max(rh, lh, rbox['h']) + 8
    for blocks, h in ((rb, rh), (lb, lh)):
        off = (H - h) / 2
        for i, b1, l2, sign in blocks:
            b1['y'] += off
            for b in l2:
                b['y'] += off
            placed.append((i, b1, l2, sign))
    rbox['y'] = (H - rbox['h']) / 2
    out.boxes.append(rbox)
    for i, b1, l2, sign in placed:
        out.boxes.append(b1)
        out.boxes += l2
        sx = rbox['x'] + rbox['w'] if sign > 0 else rbox['x']
        sy = rbox['y'] + rbox['h'] / 2
        ex = b1['x'] if sign > 0 else b1['x'] + b1['w']
        ey = b1['y'] + b1['h'] / 2
        mx = (sx + ex) / 2
        out.shapes.append({'kind': 'polyline', 'points': _bezier((sx, sy), (mx, sy), (mx, ey), (ex, ey)),
                           'stroke': _pal(i), 'width': 1.6, 'arrow': False})
        for b in l2:
            sx2 = b1['x'] + b1['w'] if sign > 0 else b1['x']
            ex2 = b['x'] if sign > 0 else b['x'] + b['w']
            ey2 = b['y'] + b['h'] / 2
            mx2 = (sx2 + ex2) / 2
            out.shapes.append({'kind': 'polyline', 'points': _bezier((sx2, ey), (mx2, ey), (mx2, ey2), (ex2, ey2)),
                               'stroke': _pal(i), 'width': 1.0, 'arrow': False})
    out.height = H
    return out


def _chevron_points(b: dict) -> list[tuple[float, float]]:
    x, y, w, h = b['x'], b['y'], b['w'], b['h']
    t = min(12.0, h / 2, w / 4)
    pts = [(x, y), (x + w - t, y), (x + w, y + h / 2), (x + w - t, y + h), (x, y + h)]
    return pts + [(x + t, y + h / 2)] if b.get('shape') == 'chevron' else pts


def _process(block: dict, width: float) -> Layout:
    steps = block['steps']
    n = len(steps)
    out = Layout(width=width)
    gap = 3.0
    cw = (width - 8 - gap * (n - 1)) / n
    size = SIZE if n <= 5 else SMALL
    heads = []
    for i, st in enumerate(steps):
        t = min(12.0, cw / 4)
        inner = cw - (t if i == 0 else 2 * t) - 6
        b = _tbox(4 + i * (cw + gap), 4, cw, [[st['label'], True]], size=size, fill=_pal(i), alpha=0.26,
                  stroke=_pal(i), shape='pentagon' if i == 0 else 'chevron', valign='middle', min_h=36,
                  wrap_w=max(20.0, inner), dx=(-t / 2 if i == 0 else 0.0))
        heads.append(b)
    hc = max(b['h'] for b in heads)
    for b in heads:
        b['h'] = hc
    out.boxes += heads
    below = 0.0
    for i, st in enumerate(steps):
        if st.get('detail'):
            d = _tbox(4 + i * (cw + gap) + 2, hc + 10, cw - 4, [[st['detail'], False]], size=SMALL, pad=2)
            out.boxes.append(d)
            below = max(below, d['h'] + 6)
    out.height = hc + 8 + below
    return out


def _comparison(block: dict, width: float) -> Layout:
    cols = block['columns']
    m = len(cols)
    out = Layout(width=width)
    gap = 12.0
    cw = (width - 8 - gap * (m - 1)) / m
    heads = [_tbox(4 + i * (cw + gap), 4, cw, [[c['label'], True]], size=SIZE + 1, fill=_pal(i), alpha=0.28,
                   stroke=_pal(i), valign='middle', min_h=26, pad=5) for i, c in enumerate(cols)]
    hh = max(b['h'] for b in heads)
    for b in heads:
        b['h'] = hh
    bodies = [_tbox(4 + i * (cw + gap), 4 + hh + 4, cw, [['• ' + it, False] for it in c['items']] or [['', False]],
                    align='left', fill='stripe', stroke=_pal(i), min_h=40, pad=6) for i, c in enumerate(cols)]
    bh = max(b['h'] for b in bodies)
    for b in bodies:
        b['h'] = bh
    out.boxes = heads + bodies
    out.height = 4 + hh + 4 + bh + 4
    return out


def image_size(sha: str) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(asset_path(sha)) as im:
            return im.size
    except Exception:
        return None


def _labelled(block: dict, width: float) -> Layout:
    out = Layout(width=width)
    calls = block['callouts']
    left = sorted([c for c in calls if c['x'] < 0.5], key=lambda c: c['y'])
    right = sorted([c for c in calls if c['x'] >= 0.5], key=lambda c: c['y'])
    size = image_size(block['image']) or (4, 3)
    aspect = size[0] / max(1, size[1])
    both = bool(left) and bool(right)
    img_w = width * (0.5 if both else 0.58)
    img_h = img_w / aspect
    if img_h > width * 0.72:
        img_h = width * 0.72
        img_w = img_h * aspect
    lw = (width - img_w) / 2 - 18 if both else width - img_w - 26
    img_x = (width - img_w) / 2 if both else (width - img_w - 4 if left else 4)
    img_y = 6.0
    out.shapes.append({'kind': 'image', 'x': img_x, 'y': img_y, 'w': img_w, 'h': img_h, 'asset': block['image']})
    bottom = img_y + img_h

    def column(items, x_box, sign):
        nonlocal bottom
        prev = 2.0
        for c in items:
            b = _tbox(x_box, 0, lw, [[c['label'], False]], size=SMALL + 0.5, fill='stripe', stroke='text', pad=3,
                      align='left' if sign > 0 else 'right')
            py = img_y + c['y'] * img_h
            b['y'] = max(py - b['h'] / 2, prev)
            prev = b['y'] + b['h'] + 5
            out.boxes.append(b)
            px = img_x + c['x'] * img_w
            ex = b['x'] if sign > 0 else b['x'] + b['w']
            out.lines.append({'x1': ex, 'y1': b['y'] + b['h'] / 2, 'x2': px, 'y2': py, 'arrow': False,
                              'color': 'accent', 'width': 1.0, 'top': True})
            out.shapes.append({'kind': 'ellipse', 'x': px - 3, 'y': py - 3, 'w': 6, 'h': 6, 'fill': 'accent',
                               'stroke': 'bg', 'width': 1.0, 'top': True})
            bottom = max(bottom, b['y'] + b['h'])
    if left:
        column(left, 4.0, -1)
    if right:
        column(right, img_x + img_w + 14 if both or not left else width - lw - 4, 1)
    out.height = bottom + 6
    return out


def _stats(block: dict, width: float) -> Layout:
    stats = block['stats']
    n = len(stats)
    out = Layout(width=width)
    gap = 10.0
    cw = (width - 8 - gap * (n - 1)) / n
    tallest = 0.0
    cards = []
    for i, st in enumerate(stats):
        x = 4 + i * (cw + gap)
        y = 10.0
        icon = st.get('icon')
        if icon:
            cards.append({'kind': 'icon', 'name': icon, 'x': x + 10, 'y': y, 'size': 18.0, 'stroke': _pal(i)})
            y += 24
        value = _show(st['value'])
        vsize = 20.0 if len(wrap(value, cw - 16, BOLD, 20.0)) <= 2 else 14.0
        vb = _tbox(x + 4, y, cw - 8, [[value, True]], size=vsize, align='left', pad=4)
        y += vb['h']
        lb = _tbox(x + 4, y, cw - 8, [[st['label'], False]], align='left', pad=4, color='muted') if st['label'] else None
        y += lb['h'] if lb else 0
        out.boxes += [vb] + ([lb] if lb else [])
        tallest = max(tallest, y - 4)
    for i in range(n):
        x = 4 + i * (cw + gap)
        out.shapes.append({'kind': 'rect', 'x': x, 'y': 4, 'w': cw, 'h': tallest + 6, 'fill': 'stripe', 'alpha': 1.0,
                           'stroke': 'border', 'width': 0.8, 'radius': True})
        out.shapes.append({'kind': 'rect', 'x': x, 'y': 4, 'w': cw, 'h': 3.5, 'fill': _pal(i), 'alpha': 1.0,
                           'stroke': None, 'width': 0, 'radius': False})
    out.shapes += [dict(c, top=True) for c in cards]
    out.height = tallest + 14
    return out


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """About n round tick values covering lo..hi."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [0.0, 1.0]
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < 1e-12:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(1, n - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5 and len(ticks) < 20:
        ticks.append(round(t, 12))
        t += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 12))
    return ticks


def _scatter(block: dict, width: float) -> Layout:
    pts = block['points']
    out = Layout(width=width)
    xs, ys = [float(p['x']) for p in pts], [float(p['y']) for p in pts]
    tx, ty = nice_ticks(min(xs), max(xs)), nice_ticks(min(ys), max(ys))
    ylabels = [_show(v) for v in ty]
    ml = max(_width(t, FONT, SMALL - 0.5) for t in ylabels) + 10
    mt = 18.0 if block.get('y_label') else 8.0
    mb = 34.0 if block.get('x_label') else 22.0
    pw, ph = width - ml - 10, width * 0.5
    x0, y0 = ml, mt

    def X(v):
        return x0 + (v - tx[0]) / ((tx[-1] - tx[0]) or 1) * pw

    def Y(v):
        return y0 + ph - (v - ty[0]) / ((ty[-1] - ty[0]) or 1) * ph
    for v in tx:
        out.lines.append({'x1': X(v), 'y1': y0, 'x2': X(v), 'y2': y0 + ph, 'arrow': False, 'color': 'border',
                          'width': 0.5})
        out.labels.append({'x': X(v), 'y': y0 + ph + 11, 'text': _show(v), 'anchor': 'middle', 'bold': False,
                           'size': SMALL - 0.5})
    for v, t in zip(ty, ylabels):
        out.lines.append({'x1': x0, 'y1': Y(v), 'x2': x0 + pw, 'y2': Y(v), 'arrow': False, 'color': 'border',
                          'width': 0.5})
        out.labels.append({'x': x0 - 4, 'y': Y(v) + 2.5, 'text': t, 'anchor': 'end', 'bold': False,
                           'size': SMALL - 0.5})
    out.lines.append({'x1': x0, 'y1': y0 + ph, 'x2': x0 + pw, 'y2': y0 + ph, 'arrow': False, 'color': 'text'})
    out.lines.append({'x1': x0, 'y1': y0 + ph, 'x2': x0, 'y2': y0, 'arrow': False, 'color': 'text'})
    if block.get('x_label'):
        out.labels.append({'x': x0 + pw / 2, 'y': y0 + ph + 26, 'text': block['x_label'], 'anchor': 'middle',
                           'bold': True, 'size': SMALL})
    if block.get('y_label'):
        out.labels.append({'x': x0, 'y': mt - 7, 'text': block['y_label'], 'anchor': 'start', 'bold': True,
                           'size': SMALL})
    r = 3.2 if len(pts) <= 60 else 2.2
    named = 0
    for p in pts:
        px, py = X(float(p['x'])), Y(float(p['y']))
        out.shapes.append({'kind': 'ellipse', 'x': px - r, 'y': py - r, 'w': 2 * r, 'h': 2 * r, 'fill': 'p0',
                           'alpha': 0.85, 'stroke': 'bg', 'width': 0.6, 'top': True})
        if p.get('label') and named < SCATTER_LABELS:
            named += 1
            end = px > x0 + pw * 0.8
            out.labels.append({'x': px - 5 if end else px + 5, 'y': py - 3, 'text': p['label'],
                               'anchor': 'end' if end else 'start', 'bold': False, 'size': SMALL - 1})
    out.height = y0 + ph + mb
    return out


SCATTER_LABELS = 20      # point labels drawn on the plot; every label is in the alt text and the table
_LAYOUTS = {'cycle': _cycle, 'venn': _venn, 'pyramid': _pyramid, 'matrix': _matrix, 'mindmap': _mindmap,
            'process': _process, 'comparison': _comparison, 'labelled': _labelled, 'stat-cards': _stats,
            'scatter': _scatter}


def arrow_head(x1, y1, x2, y2, size: float = 5.0) -> list[tuple[float, float]]:
    """The three corners of an arrow head at (x2, y2), pointing along the line."""
    ang = math.atan2(y2 - y1, x2 - x1)
    left = (x2 - size * math.cos(ang - 0.45), y2 - size * math.sin(ang - 0.45))
    right = (x2 - size * math.cos(ang + 0.45), y2 - size * math.sin(ang + 0.45))
    return [(x2, y2), left, right]


def overlaps(lay: Layout, gap: float = 0.0) -> list[tuple[int, int]]:
    """Pairs of boxes that overlap, or come closer than `gap` (a layout check for tests and the fit loop)."""
    out = []
    t = 0.5 - gap
    for i, a in enumerate(lay.boxes):
        for j in range(i + 1, len(lay.boxes)):
            b = lay.boxes[j]
            if a['x'] < b['x'] + b['w'] - t and b['x'] < a['x'] + a['w'] - t and \
                    a['y'] < b['y'] + b['h'] - t and b['y'] < a['y'] + a['h'] - t:
                out.append((i, j))
    return out


# ---------- backends ----------


def corner(theme: dict, box: dict) -> float:
    """The corner radius of a diagram box in layout points: the theme's (a design file's) radius, 3 by default, 0 for
    square corners, never more than half the box."""
    try:
        r = float(theme.get('radius', 3) if theme.get('radius') is not None else 3)
    except (TypeError, ValueError):
        r = 3.0
    return max(0.0, min(r, box['h'] / 2, box['w'] / 2)) if r == r else 3.0


_HEX = re.compile(r'[0-9A-Fa-f]{6}')


def color_of(theme: dict, key, default: str = 'text') -> str | None:
    """RRGGBB for a colour key: a theme role ("text", "stripe", "accent"), "p<n>" for the n-th palette colour, None
    for no colour. Unknown keys use `default`'s colour."""
    if key is None:
        return None
    if isinstance(key, str) and len(key) >= 2 and key[0] == 'p' and key[1:].isdigit():
        pal = [c for c in theme.get('palette') or [] if isinstance(c, str) and _HEX.fullmatch(c)]
        pal = pal or [theme.get('accent') or theme.get('text') or '000000']
        return pal[int(key[1:]) % len(pal)]
    v = theme.get(key)
    if isinstance(v, str) and _HEX.fullmatch(v):
        return v
    d = theme.get(default)
    return d if isinstance(d, str) and _HEX.fullmatch(d) else '000000'


def _box_style(b: dict) -> tuple:
    """(fill key, stroke key, alpha, shape) of a box; legacy boxes are stripe-filled and outlined in text colour."""
    return b.get('fill', 'stripe'), b.get('stroke', 'text'), float(b.get('alpha', 1.0)), b.get('shape', 'rect')


def _baselines(b: dict) -> list[tuple[float, float, str, bool]]:
    """(x, baseline y, anchor, bold) of each text line of a box, in layout points (y down)."""
    size, lead, pad = b.get('size', SIZE), b.get('lead', LEAD), b.get('pad', PAD)
    n = len(b['lines'])
    top = b['y'] + pad if b.get('valign') != 'middle' else b['y'] + (b['h'] - n * lead) / 2 + (lead - size) / 2
    align = b.get('align', 'center')
    dx = b.get('dx', 0.0)
    x, anchor = {'left': (b['x'] + pad + dx, 'start'), 'right': (b['x'] + b['w'] - pad + dx, 'end')}.get(
        align, (b['x'] + b['w'] / 2 + dx, 'middle'))
    bolds = b.get('bolds') or [b['bold']] * n
    return [(x, top + size + i * lead - 1, anchor, bolds[i] if i < len(bolds) else b['bold']) for i in range(n)]


def _icon_lines(name: str, size: float) -> list[list[tuple[float, float]]]:
    try:
        from ..studio import icons
        ic = icons.get(name)
        return icons.polylines(ic, size) if ic is not None else []
    except Exception:
        return []


def _image_pil(sha: str, grey: bool):
    from PIL import Image
    try:
        im = Image.open(asset_path(sha))
        im.load()
    except Exception:
        return None
    im = im.convert('RGB')
    return im.convert('L').convert('RGB') if grey else im


def _layers(lay: Layout):
    """Paint order: shapes under everything, lines, boxes, the shapes and lines marked top, labels."""
    return ([s for s in lay.shapes if not s.get('top')], [ln for ln in lay.lines if not ln.get('top')],
            [s for s in lay.shapes if s.get('top')], [ln for ln in lay.lines if ln.get('top')])


def pdf_drawing(block: dict, theme: dict, width: float, *, max_height: float | None = None, fit=None,
                font: str = FONT, bold: str = BOLD):
    """A reportlab Drawing of the diagram, `width` points wide (scaled down to max_height when taller). `fit(text) ->
    (text, font or None)` swaps in a font that can draw the text."""
    from reportlab.graphics.shapes import Drawing, Ellipse, Group, Image, Line, PolyLine, Polygon, Rect, String
    from reportlab.lib import colors

    lay = layout(block, width)
    scale = min(1.0, (max_height / lay.height) if max_height and lay.height > max_height else 1.0)
    text_c, fill_c, bg_c = (colors.HexColor('#' + theme[k]) for k in ('text', 'stripe', 'bg'))
    H = lay.height
    g = Group()

    def col(key):
        h = color_of(theme, key)
        return colors.HexColor('#' + h) if h else None

    def face(text, is_bold):
        if fit is None:
            return text, bold if is_bold else font
        t, f = fit(text, is_bold)
        return t, f or (bold if is_bold else font)

    def flip(pts):
        return [v for x, y in pts for v in (x, H - y)]

    def shape(s):
        k = s['kind']
        fill, stroke = col(s.get('fill')), col(s.get('stroke'))
        style = {'fillColor': fill, 'strokeColor': stroke, 'strokeWidth': s.get('width', 0.8) if stroke else 0,
                 'fillOpacity': s.get('alpha', 1.0)}
        if k == 'rect':
            r = min(corner(theme, s), 6.0) if s.get('radius') else 0
            g.add(Rect(s['x'], H - s['y'] - s['h'], s['w'], s['h'], rx=r, ry=r, **style))
        elif k == 'ellipse':
            g.add(Ellipse(s['x'] + s['w'] / 2, H - s['y'] - s['h'] / 2, s['w'] / 2, s['h'] / 2, **style))
        elif k == 'polygon':
            g.add(Polygon(flip(s['points']), **style))
        elif k == 'polyline':
            g.add(PolyLine(flip(s['points']), strokeColor=stroke, strokeWidth=s.get('width', 1.0), strokeLineCap=1,
                           strokeLineJoin=1))
            if s.get('arrow') and len(s['points']) >= 2:
                (ax, ay), (bx, by) = s['points'][-2], s['points'][-1]
                g.add(Polygon(flip(arrow_head(ax, ay, bx, by, 5.5)), fillColor=stroke, strokeColor=stroke,
                              strokeWidth=0.5))
        elif k == 'image':
            im = _image_pil(s['asset'], bool(theme.get('grey_images')))
            if im is not None:
                g.add(Image(s['x'], H - s['y'] - s['h'], s['w'], s['h'], im))
        elif k == 'icon':
            c = col(s.get('stroke', 'text'))
            for ln in _icon_lines(s['name'], s['size']):
                g.add(PolyLine(flip([(s['x'] + x, s['y'] + y) for x, y in ln]), strokeColor=c,
                               strokeWidth=2.0 * s['size'] / 24, strokeLineCap=1, strokeLineJoin=1))

    def line(ln):
        c = col(ln.get('color', 'text'))
        g.add(Line(ln['x1'], H - ln['y1'], ln['x2'], H - ln['y2'], strokeColor=c, strokeWidth=ln.get('width', 0.8)))
        if ln['arrow']:
            pts = arrow_head(ln['x1'], H - ln['y1'], ln['x2'], H - ln['y2'])
            g.add(Polygon([v for p in pts for v in p], fillColor=c, strokeColor=c, strokeWidth=0.5))
    under, lines_under, over, lines_over = _layers(lay)
    for s in under:
        shape(s)
    for ln in lines_under:
        line(ln)
    for b in lay.boxes:
        fill, stroke, alpha, kind = _box_style(b)
        style = {'fillColor': col(fill), 'strokeColor': col(stroke), 'strokeWidth': 0.8 if stroke else 0,
                 'fillOpacity': alpha}
        if 'fill' not in b and 'stroke' not in b:   # a legacy box, drawn exactly as before
            r = corner(theme, b)
            g.add(Rect(b['x'], H - b['y'] - b['h'], b['w'], b['h'], rx=r, ry=r, fillColor=fill_c, strokeColor=text_c,
                       strokeWidth=0.8))
        elif fill or stroke:
            if kind in ('chevron', 'pentagon'):
                g.add(Polygon(flip(_chevron_points(b)), **style))
            elif kind == 'ellipse':
                g.add(Ellipse(b['x'] + b['w'] / 2, H - b['y'] - b['h'] / 2, b['w'] / 2, b['h'] / 2, **style))
            else:
                r = corner(theme, b)
                g.add(Rect(b['x'], H - b['y'] - b['h'], b['w'], b['h'], rx=r, ry=r, **style))
        tc = col(b.get('color', 'text'))
        size = b.get('size', SIZE)
        for line_text, (x, base, anchor, is_bold) in zip(b['lines'], _baselines(b)):
            t, f = face(line_text, is_bold)
            g.add(String(x, H - base, t, fontName=f, fontSize=size, fillColor=tc, textAnchor=anchor))
    for s in over:
        shape(s)
    for ln in lines_over:
        line(ln)
    for lab in lay.labels:
        t, f = face(lab['text'], lab['bold'])
        if lab.get('bg'):
            w = _width(t, f, lab['size']) + 4
            x0 = lab['x'] - w / 2 if lab['anchor'] == 'middle' else lab['x'] - (w if lab['anchor'] == 'end' else 0)
            g.add(Rect(x0, H - lab['y'] - 2, w, lab['size'] + 3, fillColor=bg_c, strokeColor=None))
        g.add(String(lab['x'], H - lab['y'], t, fontName=f, fontSize=lab['size'],
                     fillColor=col(lab.get('color', 'text')), textAnchor=lab['anchor']))
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
        h = color_of(theme, k)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if h else None
    text_c, fill_c, bg_c = rgb('text'), rgb('stripe'), rgb('bg')
    img = Image.new('RGBA', (W, H), (*bg_c, 255))
    d = ImageDraw.Draw(img)
    fonts = {False: _pil_font(SIZE * s * 0.9), True: _pil_font(SIZE * s * 0.9, True)}
    lw = max(1, round(0.8 * s))

    def P(pts):
        return [(x * s, y * s) for x, y in pts]

    def solid(fn, fill, stroke, alpha, width):
        """Draw with fn(draw, fill, outline, width), through a layer when the fill is see-through."""
        nonlocal d
        if fill is not None and alpha < 0.8:
            layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            fn(ImageDraw.Draw(layer), (*fill, int(255 * alpha)), None, 0)
            img.alpha_composite(layer)
            d = ImageDraw.Draw(img)
            if stroke is not None:
                fn(d, None, stroke, width)
        else:
            fn(d, fill, stroke, width)

    def shape(sh):
        k = sh['kind']
        fill, stroke, alpha = rgb(sh.get('fill')), rgb(sh.get('stroke')), float(sh.get('alpha', 1.0))
        width = max(1, round(sh.get('width', 0.8) * s))
        if k in ('rect', 'ellipse'):
            box = [sh['x'] * s, sh['y'] * s, (sh['x'] + sh['w']) * s, (sh['y'] + sh['h']) * s]
            if k == 'ellipse':
                solid(lambda dr, f, o, w: dr.ellipse(box, fill=f, outline=o, width=w), fill, stroke, alpha, width)
            else:
                r = min(corner(theme, sh), 6.0) * s if sh.get('radius') else 0
                solid(lambda dr, f, o, w: dr.rounded_rectangle(box, radius=r, fill=f, outline=o, width=w), fill, stroke,
                      alpha, width)
        elif k == 'polygon':
            pts = P(sh['points'])
            solid(lambda dr, f, o, w: dr.polygon(pts, fill=f, outline=o, width=w), fill, stroke, alpha, width)
        elif k == 'polyline':
            d.line(P(sh['points']), fill=stroke, width=width, joint='curve')
            if sh.get('arrow') and len(sh['points']) >= 2:
                (ax, ay), (bx, by) = sh['points'][-2], sh['points'][-1]
                d.polygon(P(arrow_head(ax, ay, bx, by, 5.5)), fill=stroke)
        elif k == 'image':
            im = _image_pil(sh['asset'], bool(theme.get('grey_images')))
            if im is not None:
                w, h = max(1, round(sh['w'] * s)), max(1, round(sh['h'] * s))
                img.paste(im.resize((w, h), Image.LANCZOS), (round(sh['x'] * s), round(sh['y'] * s)))
        elif k == 'icon':
            c = rgb(sh.get('stroke', 'text'))
            for ln in _icon_lines(sh['name'], sh['size'] * s):
                d.line([(sh['x'] * s + x, sh['y'] * s + y) for x, y in ln], fill=c,
                       width=max(1, round(2.0 * sh['size'] * s / 24)), joint='curve')

    def line(ln):
        c = rgb(ln.get('color', 'text'))
        d.line([(ln['x1'] * s, ln['y1'] * s), (ln['x2'] * s, ln['y2'] * s)], fill=c,
               width=max(1, round(ln.get('width', 0.8) * s)))
        if ln['arrow']:
            d.polygon([(x * s, y * s) for x, y in arrow_head(ln['x1'], ln['y1'], ln['x2'], ln['y2'])], fill=c)
    under, lines_under, over, lines_over = _layers(lay)
    for sh in under:
        shape(sh)
    for ln in lines_under:
        line(ln)
    for b in lay.boxes:
        fill, stroke, alpha, kind = _box_style(b)
        rect = [b['x'] * s, b['y'] * s, (b['x'] + b['w']) * s, (b['y'] + b['h']) * s]
        fc, sc = rgb(fill), rgb(stroke)
        if fc is not None or sc is not None:
            if kind in ('chevron', 'pentagon'):
                pts = P(_chevron_points(b))
                solid(lambda dr, f, o, w: dr.polygon(pts, fill=f, outline=o, width=w), fc, sc, alpha, lw)
            elif kind == 'ellipse':
                solid(lambda dr, f, o, w: dr.ellipse(rect, fill=f, outline=o, width=w), fc, sc, alpha, lw)
            else:
                r = corner(theme, b)
                if r > 0:
                    solid(lambda dr, f, o, w: dr.rounded_rectangle(rect, radius=r * s, fill=f, outline=o, width=w), fc,
                          sc, alpha, lw)
                else:
                    solid(lambda dr, f, o, w: dr.rectangle(rect, fill=f, outline=o, width=w), fc, sc, alpha, lw)
        tc = rgb(b.get('color', 'text'))
        size = b.get('size', SIZE)
        fs = fonts if size == SIZE else {False: _pil_font(size * s * 0.9), True: _pil_font(size * s * 0.9, True)}
        for text, (x, base, anchor, is_bold) in zip(b['lines'], _baselines(b)):
            d.text((x * s, base * s), text, font=fs[bool(is_bold)], fill=tc,
                   anchor={'middle': 'ms', 'start': 'ls', 'end': 'rs'}[anchor])
    for sh in over:
        shape(sh)
    for ln in lines_over:
        line(ln)
    for lab in lay.labels:
        f = _pil_font(lab['size'] * s * 0.9, lab['bold'])
        anchor = {'middle': 'ms', 'start': 'ls', 'end': 'rs'}[lab['anchor']]
        if lab.get('bg'):
            box = d.textbbox((lab['x'] * s, lab['y'] * s), lab['text'], font=f, anchor=anchor)
            d.rectangle([box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1], fill=bg_c)
        d.text((lab['x'] * s, lab['y'] * s), lab['text'], font=f, fill=rgb(lab.get('color', 'text')), anchor=anchor)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'PNG', dpi=(200, 200))
    return buf.getvalue()


def _mm(text: str) -> str:
    """Label text that can't break mermaid syntax."""
    s = re.sub(r'[\r\n`<>|"{}\[\]#;]', ' ', str(text))
    return re.sub(r'\s+', ' ', s).strip() or ' '


def _mm_graph(direction: str, labels: list[str], edges: list[tuple[int, int]]) -> str:
    out = [f'flowchart {direction}'] + [f'    n{i}["{_mm(t)}"]' for i, t in enumerate(labels)]
    out += [f'    n{a} --> n{b}' for a, b in edges]
    return '\n'.join(out)


def _mm_groups(heads: list[str], groups: list[list[str]], extra: list[str] = (), extra_head: str = '') -> str:
    labels, edges = [], []
    for head, items in zip(heads, groups):
        h = len(labels)
        labels.append(head)
        for it in items:
            edges.append((h, len(labels)))
            labels.append(it)
    if extra:
        h = len(labels)
        labels.append(extra_head)
        for it in extra:
            edges.append((h, len(labels)))
            labels.append(it)
    return _mm_graph('TD', labels, edges)


def mermaid(block: dict) -> str:
    kind = block['type']
    if kind in ('cycle', 'pyramid', 'process'):
        labels = block['steps'] if kind == 'cycle' else block['levels'] if kind == 'pyramid' else \
            [f'{s["label"]}: {s["detail"]}' if s.get('detail') else s['label'] for s in block['steps']]
        edges = [(i, i + 1) for i in range(len(labels) - 1)] + ([(len(labels) - 1, 0)] if kind == 'cycle' else [])
        return _mm_graph('TD' if kind == 'pyramid' else 'LR', labels, edges)
    if kind in ('venn', 'comparison', 'matrix'):
        groups = block['sets' if kind == 'venn' else 'columns' if kind == 'comparison' else 'quadrants']
        heads = [g['label'] or name for g, name in zip(groups, QUADRANT_NAMES)] if kind == 'matrix' else \
            [g['label'] for g in groups]
        return _mm_groups(heads, [g['items'] for g in groups], block.get('shared') or [], 'Shared')
    if kind == 'mindmap':
        ids = [n['id'] for n in block['nodes']]
        return _mm_graph('LR', [n['label'] for n in block['nodes']],
                         [(ids.index(n['parent']), i) for i, n in enumerate(block['nodes']) if n['parent'] in ids])
    if kind == 'labelled':
        return _mm_groups([block.get('title') or 'Figure'], [[c['label'] for c in block['callouts']]])
    if kind == 'stat-cards':
        return _mm_graph('LR', [f'{_show(s["value"])} {s["label"]}'.strip() for s in block['stats']], [])
    if kind == 'scatter':
        pts = [p for p in block['points'] if p.get('label')][:30] or block['points'][:30]
        return _mm_graph('LR', [f'{p.get("label") or ""} ({_show(p["x"])}, {_show(p["y"])})'.strip() for p in pts], [])
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
    """Native shapes and connectors in a group named "Diagram: <title>", fitted into box (x, y, w, h in inches). Every
    part is an editable PowerPoint shape: boxes, chevrons and ellipses are autoshapes, curves and icons freeforms."""
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
        h = color_of(theme, k)
        return RGBColor(*(int(h[i:i + 2], 16) for i in (0, 2, 4))) if h else None
    text_c, fill_c = rgb('text'), rgb('stripe')
    group = slide.shapes.add_group_shape()
    group.name = f'Diagram: {block.get("title") or block["type"]}'[:200]

    def alpha(shp, a):
        if a >= 0.999:
            return
        solid = shp._element.spPr.find(qn('a:solidFill'))
        if solid is not None and len(solid):
            etree.SubElement(solid[0], qn('a:alpha')).set('val', str(int(max(0.0, a) * 100000)))

    def paint(shp, fill, stroke, a=1.0, width=1.0):
        if fill is not None:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
            alpha(shp, a)
        else:
            shp.fill.background()
        if stroke is not None:
            shp.line.color.rgb, shp.line.width = rgb(stroke), Pt(max(0.25, width * min(s, 1.5)))
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False

    def freeform(pts, closed):
        pts = [(int((ox + x * s) * 12700), int((oy + y * s) * 12700)) for x, y in pts]
        ff = group.shapes.build_freeform(pts[0][0], pts[0][1], scale=1.0)
        ff.add_line_segments(pts[1:], close=closed)
        return ff.convert_to_shape()

    def arrow(shp):
        tail = etree.SubElement(shp.line._get_or_add_ln(), qn('a:tailEnd'))
        tail.set('type', 'triangle')

    def shape(sh):
        k = sh['kind']
        if k in ('rect', 'ellipse'):
            kind = MSO_SHAPE.OVAL if k == 'ellipse' else (MSO_SHAPE.ROUNDED_RECTANGLE if sh.get('radius')
                                                          else MSO_SHAPE.RECTANGLE)
            shp = group.shapes.add_shape(kind, emu(ox + sh['x'] * s), emu(oy + sh['y'] * s), emu(max(0.5, sh['w'] * s)),
                                         emu(max(0.5, sh['h'] * s)))
            if kind == MSO_SHAPE.ROUNDED_RECTANGLE:
                shp.adjustments[0] = min(0.5, min(corner(theme, sh), 6.0) / max(min(sh['w'], sh['h']), 1))
            paint(shp, sh.get('fill'), sh.get('stroke'), float(sh.get('alpha', 1.0)), sh.get('width', 1.0))
            shp.name = 'Diagram shape'
        elif k in ('polygon', 'polyline'):
            if len(sh['points']) < 2:
                return
            shp = freeform(sh['points'], k == 'polygon')
            paint(shp, sh.get('fill') if k == 'polygon' else None, sh.get('stroke'), float(sh.get('alpha', 1.0)),
                  sh.get('width', 1.0))
            if k == 'polyline' and sh.get('arrow'):
                arrow(shp)
            shp.name = 'Diagram connector' if k == 'polyline' else 'Diagram shape'
        elif k == 'image':
            import io as _io
            im = _image_pil(sh['asset'], bool(theme.get('grey_images')))
            if im is None:
                return
            buf = _io.BytesIO()
            im.save(buf, 'PNG')
            buf.seek(0)
            pic = group.shapes.add_picture(buf, emu(ox + sh['x'] * s), emu(oy + sh['y'] * s), emu(sh['w'] * s),
                                           emu(sh['h'] * s))
            pic.name = 'Labelled figure'
            pic._element.nvPicPr.cNvPr.set('descr', alt_text(block)[:1000])
        elif k == 'icon':
            for ln in _icon_lines(sh['name'], sh['size']):
                if len(ln) < 2:
                    continue
                shp = freeform([(sh['x'] + x, sh['y'] + y) for x, y in ln], False)
                paint(shp, None, sh.get('stroke', 'text'), 1.0, 2.0 * sh['size'] / 24)
                shp.name = f'Icon: {sh["name"]}'

    def line(ln):
        c = group.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, emu(ox + ln['x1'] * s), emu(oy + ln['y1'] * s),
                                       emu(ox + ln['x2'] * s), emu(oy + ln['y2'] * s))
        c.line.color.rgb = rgb(ln.get('color', 'text'))
        c.line.width = Pt(1) if 'width' not in ln else Pt(max(0.25, ln['width'] * min(s, 1.5)))
        if ln['arrow']:
            tail = etree.SubElement(c.line._get_or_add_ln(), qn('a:tailEnd'))
            tail.set('type', 'triangle')
    under, lines_under, over, lines_over = _layers(lay)
    for sh in under:
        shape(sh)
    for ln in lines_under:
        line(ln)
    size = max(8.0, SIZE * s)
    kinds = {'rect': MSO_SHAPE.RECTANGLE, 'ellipse': MSO_SHAPE.OVAL, 'chevron': MSO_SHAPE.CHEVRON,
             'pentagon': MSO_SHAPE.PENTAGON}
    for b in lay.boxes:
        legacy = 'fill' not in b and 'stroke' not in b
        fill, stroke, a, kind = _box_style(b)
        r = corner(theme, b) if kind == 'rect' and (fill or stroke) else 0
        shp = group.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if r > 0 else kinds.get(kind, MSO_SHAPE.RECTANGLE),
                                     emu(ox + b['x'] * s), emu(oy + b['y'] * s), emu(b['w'] * s), emu(b['h'] * s))
        if r > 0:  # the adjustment is the radius as a share of the shorter side
            shp.adjustments[0] = min(0.5, r / max(min(b['w'], b['h']), 1))
        if kind in ('chevron', 'pentagon'):
            shp.adjustments[0] = min(12.0, b['h'] / 2, b['w'] / 4) / max(min(b['w'], b['h']), 1)
        if legacy:
            shp.fill.solid()
            shp.fill.fore_color.rgb = fill_c
            shp.line.color.rgb, shp.line.width = text_c, Pt(1)
            shp.shadow.inherit = False
        else:
            paint(shp, fill, stroke, a)
        tf = shp.text_frame
        tf.word_wrap = True
        pad = b.get('pad', PAD)
        notch = min(12.0, b['h'] / 2, b['w'] / 4) if kind in ('chevron', 'pentagon') else 0
        tf.margin_left = emu((pad * 0.5 + (notch if kind == 'chevron' else 0)) * s)
        tf.margin_right = emu((pad * 0.5 + notch) * s)
        tf.margin_top = tf.margin_bottom = emu(pad * s * 0.3)
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE if legacy or b.get('valign') == 'middle' else MSO_ANCHOR.TOP
        align = {'left': PP_ALIGN.LEFT, 'right': PP_ALIGN.RIGHT}.get(b.get('align'), PP_ALIGN.CENTER)
        tc = rgb(b.get('color', 'text'))
        bsize = size if 'size' not in b else max(7.0, b['size'] * s)
        paras = b.get('paras') or [[b['label'], b['bold']]]
        for k, (text, is_bold) in enumerate(paras):
            p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            p.alignment = align
            run = p.add_run()
            run.text = text
            run.font.size, run.font.color.rgb, run.font.bold = Pt(bsize * 0.92), tc, is_bold or None
            if font:
                run.font.name = font
        shp.name = 'Diagram box' if not legacy else shp.name
    for sh in over:
        shape(sh)
    for ln in lines_over:
        line(ln)
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
        r.font.size, r.font.color.rgb, r.font.bold = Pt(max(7.0, lab['size'] * s * 0.92)), \
            rgb(lab.get('color', 'text')), lab['bold'] or None
        if font:
            r.font.name = font
