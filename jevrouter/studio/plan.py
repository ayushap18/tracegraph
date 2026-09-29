"""The Studio contract's shared data (docs/PLAN-designer.md section 9): the DesignPlan and its parts, the art direction,
critic edits, freeform shape programs, QA results and the design report, each as a dataclass with to_dict/from_dict and
a JSON schema, plus a small validator for those schemas (no jsonschema dependency).

Every Studio module reads and writes these types. Changing a field here is a contract change: additive only, and every
builder is told. Units are PDF points (1/72 in) unless a name says otherwise; colours are token names, never hex.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLAN_VERSION = 1
FORMATS = ('pdf', 'docx', 'pptx', 'xlsx', 'md')
PAINTED = ('pptx', 'pdf')        # formats Studio positions and paints itself; the others get flow styling
BOX_KINDS = ('text', 'image', 'icon', 'diagram', 'chart', 'table', 'shape')
ALIGN = ('left', 'center', 'right', 'justify')
VALIGN = ('top', 'middle', 'bottom')
FIT = ('cover', 'contain')
TYPE_STEPS = ('caption', 'body', 'lead', 'h3', 'h2', 'h1', 'display')
# token colour names a box, a freeform shape or a critic edit may use (tokens.DesignSystem.colors keys + chart1..6)
TOKEN_COLORS = ('bg', 'surface', 'text', 'heading', 'muted', 'accent', 'accent2', 'border', 'header_bg', 'header_text',
                'stripe', 'code_bg', 'overlay', 'on_accent', 'chart1', 'chart2', 'chart3', 'chart4', 'chart5', 'chart6')
STOP_REASONS = ('pass', 'rounds', 'budget', 'deadline', 'error', 'keyless')
CHECK_IDS = ('D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8')
PHASES = ('direct', 'assets', 'layout', 'thumbs', 'qa', 'fix', 'critic', 'freeform', 'paint')

# content refs (Box.content): what a box shows, never the text itself
#   spec:title | spec:subtitle | spec:<s>/heading | spec:<s>/notes | spec:<s>/<b> | spec:<s>/<b>/items[<i>:<j>]
#   text:<literal, at most 120 chars, only for generated labels like "(continued)" or a slide number>
#   asset:<sha256>  (create/assets.py cache)  ws:<kind>/<name>  (workspace)  icon:<lucide name>  art:<kind>:<seed>
#   none
# Amendment (builder L, additive): a spec ref may also slice and pick a field, resolved by layout.box_text:
#   spec:<s>/<b>/rows[<i>:<j>]   table rows (the header row is always drawn)
#   .../words[<i>:<j>]           a word range of a paragraph, quote or item (text split across slides/pages)
#   ...#<field>                  REF_FIELDS: caption, credit, by, title, summary (chart), stat, rest (a statistic and
#                                the words after it), continued ("<heading> (continued)"), footer (the running title)
REF_PREFIXES = ('spec:', 'text:', 'asset:', 'ws:', 'icon:', 'art:', 'none')
REF_FIELDS = ('caption', 'credit', 'by', 'title', 'summary', 'stat', 'rest', 'continued', 'footer')


# ---------- the DesignPlan ----------


@dataclass
class BoxStyle:
    fill: str | None = None          # token colour
    stroke: str | None = None        # token colour
    stroke_w: float = 0.0            # points
    text_color: str | None = None    # token colour (text boxes)
    radius: float = 0.0              # points
    opacity: float = 1.0             # 0..1 (overlays)
    shadow: bool = False


@dataclass
class Box:
    id: str                          # unique within the page, e.g. 'p3.title'
    kind: str                        # BOX_KINDS
    x: float
    y: float                         # top-left origin, points
    w: float
    h: float
    z: int = 0                       # paint order, low first
    slot: str | None = None          # library slot name the box fills (None for freeform/decorative boxes)
    content: str = 'none'            # content ref (REF_PREFIXES)
    style: BoxStyle = field(default_factory=BoxStyle)
    font: str | None = None          # family (text boxes); resolved by studio/fonts.py
    step: str | None = None          # TYPE_STEPS name the size came from
    size: float | None = None        # points, after fitting
    bold: bool = False
    italic: bool = False
    line_height: float = 1.2         # multiple of size
    align: str = 'left'
    valign: str = 'top'
    fit: str | None = None           # FIT, image-like boxes
    crop: tuple[float, float, float, float] | None = None   # (x, y, w, h) fractions of the source image
    focal: tuple[float, float] | None = None                # (x, y) fractions
    lines: list[str] | None = None   # measured wrapped lines (text), written by the layout engine
    alt: str | None = None           # alt text for image/icon/diagram/chart boxes
    overlay_ok: bool = False         # may overlap boxes below it by design (text on an overlay, art behind type)
    bleed: bool = False              # may touch the page edge (ignores safe margins)

    @classmethod
    def from_dict(cls, d: dict) -> 'Box':
        d = dict(d)
        d['style'] = BoxStyle(**(d.get('style') or {}))
        for k in ('crop', 'focal'):
            if d.get(k) is not None:
                d[k] = tuple(d[k])
        return cls(**d)


@dataclass
class PagePlan:
    index: int                       # 0-based position in the file
    layout: str                      # library.SLIDE_LAYOUTS / PAGE_TEMPLATES id, or 'freeform'
    w: float                         # page size in points
    h: float
    section: int | None = None       # DocSpec section index it shows (None: cover/closing/credits made by Studio)
    variant: str = 'default'         # 'default' | 'compact'
    freeform: bool = False
    background: str = 'bg'           # token colour, or an image/art content ref
    continued: bool = False          # a split page ("(continued)")
    notes: str = ''                  # speaker notes (pptx) text
    boxes: list[Box] = field(default_factory=list)
    direction: int | None = None     # amendment (L): index into the plan's direction pages this page came from

    @classmethod
    def from_dict(cls, d: dict) -> 'PagePlan':
        d = dict(d)
        d['boxes'] = [Box.from_dict(b) for b in d.get('boxes') or []]
        return cls(**d)


@dataclass
class FontUse:
    family: str                      # the family the file names
    role: str                        # 'display' | 'heading' | 'body' | 'caption' | 'mono'
    source: str                      # fonts.SOURCES
    licence: str                     # fonts.LICENCES_OK value, 'system', 'user' or 'builtin'
    embedded: bool                   # true only for PDF subsets
    fallback: str | None = None      # what PowerPoint/Word shows when the family isn't installed
    requested: str | None = None     # what the user asked for, when different
    note: str | None = None          # honest sentence when it differs from the request


@dataclass
class AssetUse:
    ref: str                         # content ref (asset:/ws:/icon:/art:)
    kind: str                        # 'image' | 'icon' | 'diagram' | 'art' | 'chart'
    source: str                      # 'commons' | 'lucide' | 'studio' | 'user'
    licence: str                     # e.g. 'CC BY-SA 4.0', 'ISC', 'generated'
    credit: str | None = None


@dataclass
class QaResult:
    id: str                          # CHECK_IDS
    ok: bool
    note: str = ''
    page: int | None = None          # 0-based page index; None: whole file
    box: str | None = None           # Box.id
    value: float | None = None       # what was measured
    threshold: float | None = None   # the limit it was held to
    fixed: bool = False              # a code fix or critic edit resolved it in a later round


@dataclass
class DesignPlan:
    file_id: str
    format: str                      # FORMATS
    preset: str                      # presets.PRESETS id (or 'custom' when a design.md defined everything)
    system: dict                     # tokens.DesignSystem.to_dict()
    direction: dict                  # ArtDirection.to_dict()
    pages: list[PagePlan] = field(default_factory=list)   # empty for docx/md/xlsx
    flow: dict | None = None         # docx/md/xlsx: FLOW_SCHEMA
    fonts: list[FontUse] = field(default_factory=list)
    assets: list[AssetUse] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    qa: list[QaResult] = field(default_factory=list)
    score: int | None = None         # 0..100 after the last QA round
    rounds: int = 0                  # agent loop rounds run
    stop: str | None = None          # STOP_REASONS
    tokens: dict = field(default_factory=lambda: {'direct_in': 0, 'direct_out': 0, 'critic_in': 0, 'critic_out': 0,
                                                  'freeform_in': 0, 'freeform_out': 0})
    version: int = PLAN_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'DesignPlan':
        d = dict(d)
        d['pages'] = [PagePlan.from_dict(p) for p in d.get('pages') or []]
        d['fonts'] = [FontUse(**f) for f in d.get('fonts') or []]
        d['assets'] = [AssetUse(**a) for a in d.get('assets') or []]
        d['qa'] = [QaResult(**q) for q in d.get('qa') or []]
        return cls(**d)


# ---------- art direction ----------


@dataclass
class PageDirection:
    layout: str                      # library id
    image: int | None = None         # index into the spec's image blocks (document order)
    emphasis: str | None = None      # EMPHASIS
    focus: str | None = None         # 'block:<n>' within the page's section
    variant: str = 'default'
    freeform: bool = False


EMPHASIS = ('title', 'image', 'number', 'quote', 'diagram', 'chart')


@dataclass
class ArtDirection:
    preset: str
    mood: list[str] = field(default_factory=list)
    dark: bool | None = None
    pages: list[PageDirection] = field(default_factory=list)
    source: str = 'keyless'          # 'keyless' | 'llm' | 'restyle'
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'ArtDirection':
        d = dict(d)
        d['pages'] = [PageDirection(**p) for p in d.get('pages') or []]
        return cls(**d)


# ---------- critic edits and freeform programs ----------

CRITIC_ACTIONS = ('change_layout', 'emphasize', 'reduce_text', 'swap_image', 'recolor_accent', 'enlarge_title',
                  'add_icon')


@dataclass
class CriticEdit:
    page: int                        # 1-based, as the model sees the contact sheet
    action: str                      # CRITIC_ACTIONS
    arg: str | int | None = None     # see CRITIC_SCHEMA


FREEFORM_PRIMITIVES = ('rect', 'ellipse', 'line', 'path', 'text', 'image', 'icon', 'diagram')


@dataclass
class ShapeProgram:
    page: int                        # 0-based page index it replaces
    cols: int
    rows: int
    shapes: list[dict]               # FREEFORM_SCHEMA items, validated


# ---------- the agent's result ----------


@dataclass
class RestyleOptions:
    preset: str | None = None
    fonts: dict[str, str] | None = None     # role -> family ('display' | 'heading' | 'body')
    dark: bool | None = None
    template: str | None = None             # presets.TEMPLATES id
    layouts: dict[int, str] | None = None   # 0-based page index -> layout id
    print_version: bool = False             # grayscale-safe, white pages, print minimum sizes


@dataclass
class DesignResult:
    plan: DesignPlan
    painted_bytes: bytes | None      # the file, for PAINTED formats; None: the caller renders with the legacy renderer
    report: dict                     # REPORT_SCHEMA
    phases: list[dict] = field(default_factory=list)   # [{phase: PHASES, calls, llm_in, llm_out, ms}]; also report['phases']
    notes: list[str] = field(default_factory=list)     # plain sentences for the answer
    caveats: list[str] = field(default_factory=list)   # what could not be honoured
    meta_design: dict | None = None  # CreatedFile.design additions: {preset, fonts, score, notes, thumbs}
    fell_back: bool = False          # Studio failed and the legacy renderer made the file


# ---------- JSON schemas ----------

def _o(props: dict, required: tuple = (), extra: bool = False) -> dict:
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': extra}


_NUM = {'type': 'number'}
_INT = {'type': 'integer'}
_STR = {'type': 'string'}
_BOOL = {'type': 'boolean'}
_NULL_STR = {'type': ['string', 'null']}
_NULL_INT = {'type': ['integer', 'null']}
_COLOR = {'type': ['string', 'null'], 'enum': [*TOKEN_COLORS, None]}


def _layout_ids() -> list[str]:
    from .library import PAGE_TEMPLATES, SLIDE_LAYOUTS
    return [*SLIDE_LAYOUTS, *PAGE_TEMPLATES]


def _preset_ids() -> list[str]:
    from .presets import PRESETS
    return list(PRESETS)


BOX_SCHEMA = _o({
    'id': _STR, 'kind': {'type': 'string', 'enum': list(BOX_KINDS)},
    'x': _NUM, 'y': _NUM, 'w': {'type': 'number', 'minimum': 0}, 'h': {'type': 'number', 'minimum': 0}, 'z': _INT,
    'slot': _NULL_STR, 'content': _STR,
    'style': _o({'fill': _COLOR, 'stroke': _COLOR, 'stroke_w': _NUM, 'text_color': _COLOR, 'radius': _NUM,
                 'opacity': {'type': 'number', 'minimum': 0, 'maximum': 1}, 'shadow': _BOOL}),
    'font': _NULL_STR, 'step': {'type': ['string', 'null'], 'enum': [*TYPE_STEPS, None]},
    'size': {'type': ['number', 'null']}, 'bold': _BOOL, 'italic': _BOOL, 'line_height': _NUM,
    'align': {'type': 'string', 'enum': list(ALIGN)}, 'valign': {'type': 'string', 'enum': list(VALIGN)},
    'fit': {'type': ['string', 'null'], 'enum': [*FIT, None]},
    'crop': {'type': ['array', 'null'], 'items': _NUM, 'minItems': 4, 'maxItems': 4},
    'focal': {'type': ['array', 'null'], 'items': _NUM, 'minItems': 2, 'maxItems': 2},
    'lines': {'type': ['array', 'null'], 'items': _STR}, 'alt': _NULL_STR, 'overlay_ok': _BOOL, 'bleed': _BOOL,
}, ('id', 'kind', 'x', 'y', 'w', 'h'))

FLOW_SCHEMA = _o({
    'styles': {'type': 'object'},    # step -> {family, size, color, bold, italic, space_before, space_after, line_height}
    'figures': {'type': 'array', 'items': _o({'ref': _STR, 'placement': {'type': 'string',
                                                                        'enum': ['inline', 'full', 'side']}},
                                             ('ref', 'placement'))},
    'breaks': {'type': 'array', 'items': _INT},   # section indices that start on a new page
    'palette': {'type': 'array', 'items': _STR},  # chart colours (xlsx), hex RRGGBB resolved from tokens
}, ('styles',))

QA_RESULT_SCHEMA = _o({'id': {'type': 'string', 'enum': list(CHECK_IDS)}, 'ok': _BOOL, 'note': _STR,
                       'page': _NULL_INT, 'box': _NULL_STR, 'value': {'type': ['number', 'null']},
                       'threshold': {'type': ['number', 'null']}, 'fixed': _BOOL}, ('id', 'ok'))


def design_plan_schema() -> dict:
    """The DesignPlan JSON schema (layout and preset ids filled from library/presets)."""
    page = _o({
        'index': _INT, 'layout': {'type': 'string', 'enum': [*_layout_ids(), 'freeform']},
        'w': _NUM, 'h': _NUM, 'section': _NULL_INT, 'variant': {'type': 'string', 'enum': ['default', 'compact']},
        'freeform': _BOOL, 'background': _STR, 'continued': _BOOL, 'notes': _STR,
        'boxes': {'type': 'array', 'items': BOX_SCHEMA}, 'direction': _NULL_INT,
    }, ('index', 'layout', 'w', 'h', 'boxes'))
    font = _o({'family': _STR, 'role': {'type': 'string', 'enum': ['display', 'heading', 'body', 'caption', 'mono']},
               'source': _STR, 'licence': _STR, 'embedded': _BOOL, 'fallback': _NULL_STR, 'requested': _NULL_STR,
               'note': _NULL_STR}, ('family', 'role', 'source', 'licence', 'embedded'))
    asset = _o({'ref': _STR, 'kind': {'type': 'string', 'enum': ['image', 'icon', 'diagram', 'art', 'chart']},
                'source': _STR, 'licence': _STR, 'credit': _NULL_STR}, ('ref', 'kind', 'source', 'licence'))
    return _o({
        'version': {'type': 'integer', 'enum': [PLAN_VERSION]},
        'file_id': _STR, 'format': {'type': 'string', 'enum': list(FORMATS)},
        'preset': {'type': 'string', 'enum': [*_preset_ids(), 'custom']},
        'system': {'type': 'object'}, 'direction': {'type': 'object'},
        'pages': {'type': 'array', 'items': page}, 'flow': {'type': ['object', 'null']},
        'fonts': {'type': 'array', 'items': font}, 'assets': {'type': 'array', 'items': asset},
        'notes': {'type': 'array', 'items': _STR}, 'qa': {'type': 'array', 'items': QA_RESULT_SCHEMA},
        'score': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 100}, 'rounds': _INT,
        'stop': {'type': ['string', 'null'], 'enum': [*STOP_REASONS, None]},
        'tokens': {'type': 'object'},
    }, ('version', 'file_id', 'format', 'preset', 'system', 'direction', 'pages'))


def art_schema(llm: bool = True) -> dict:
    """What the art director returns. llm=True: the schema the model is given (no `source`/`notes`, which code adds)."""
    page = _o({'layout': {'type': 'string', 'enum': _layout_ids()}, 'image': _NULL_INT,
               'emphasis': {'type': ['string', 'null'], 'enum': [*EMPHASIS, None]},
               'focus': {'type': ['string', 'null'], 'pattern': r'^block:\d{1,2}$'},
               'variant': {'type': 'string', 'enum': ['default', 'compact']}, 'freeform': _BOOL}, ('layout',))
    props = {'preset': {'type': 'string', 'enum': _preset_ids()},
             'mood': {'type': 'array', 'items': _STR, 'maxItems': 4},
             'dark': {'type': ['boolean', 'null']},
             'pages': {'type': 'array', 'items': page, 'minItems': 1, 'maxItems': 60}}
    if not llm:
        props.update({'source': {'type': 'string', 'enum': ['keyless', 'llm', 'restyle']},
                      'notes': {'type': 'array', 'items': _STR}})
    return _o(props, ('preset', 'pages'))


def critic_schema() -> dict:
    """The critic returns edits only. arg by action: change_layout -> layout id; emphasize -> slot name; reduce_text ->
    max words (int 5..60); swap_image -> image index (int) or null for the next candidate; recolor_accent -> token
    colour; enlarge_title -> steps (int 1..2); add_icon -> Lucide icon name."""
    edit = _o({'page': {'type': 'integer', 'minimum': 1}, 'action': {'type': 'string', 'enum': list(CRITIC_ACTIONS)},
               'arg': {'type': ['string', 'integer', 'null']}}, ('page', 'action'))
    return _o({'edits': {'type': 'array', 'items': edit, 'maxItems': 12}}, ('edits',))


def freeform_schema(cols: int = 12, rows: int = 12) -> dict:
    """A freeform page as a shape program: primitives in grid units (0..cols, 0..rows, multiples of 0.5), colours as
    token names, text as a literal (<= 200 chars) or a spec content ref; image/diagram refs must name workspace or asset
    cache entries the page already has; icons name Lucide icons."""
    unit_x = {'type': 'number', 'minimum': 0, 'maximum': cols, 'multipleOf': 0.5}
    unit_y = {'type': 'number', 'minimum': 0, 'maximum': rows, 'multipleOf': 0.5}
    shape = _o({
        'type': {'type': 'string', 'enum': list(FREEFORM_PRIMITIVES)},
        'x': unit_x, 'y': unit_y, 'w': unit_x, 'h': unit_y, 'z': {'type': 'integer', 'minimum': 0, 'maximum': 40},
        'fill': _COLOR, 'stroke': _COLOR, 'stroke_w': {'type': 'number', 'minimum': 0, 'maximum': 4},
        'radius': {'type': 'number', 'minimum': 0, 'maximum': 1}, 'opacity': {'type': 'number', 'minimum': 0,
                                                                              'maximum': 1},
        'text': {'type': 'string', 'maxLength': 200}, 'ref': _STR,
        'step': {'type': 'string', 'enum': list(TYPE_STEPS)}, 'align': {'type': 'string', 'enum': list(ALIGN)},
        'weight': {'type': 'string', 'enum': ['regular', 'bold']},
        'points': {'type': 'array', 'maxItems': 32, 'items': {'type': 'array', 'items': _NUM, 'minItems': 2,
                                                                'maxItems': 2}},
        'alt': {'type': 'string', 'maxLength': 200},
    }, ('type', 'x', 'y', 'w', 'h'))
    return _o({'shapes': {'type': 'array', 'items': shape, 'minItems': 1, 'maxItems': 40}}, ('shapes',))


def report_schema() -> dict:
    """The design report stored with a file and served by GET /api/created/{id}/design."""
    check = _o({'id': {'type': 'string', 'enum': list(CHECK_IDS)}, 'name': _STR, 'ok': _BOOL, 'failures': _INT,
                'note': _STR}, ('id', 'name', 'ok', 'failures'))
    return _o({
        'version': {'type': 'integer', 'enum': [PLAN_VERSION]},
        'score': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        'preset': _STR, 'format': {'type': 'string', 'enum': list(FORMATS)},
        'fonts': {'type': 'array', 'items': {'type': 'object'}},
        'rounds': _INT, 'stop': {'type': 'string', 'enum': list(STOP_REASONS)},
        'tokens': {'type': 'object'},
        'checks': {'type': 'array', 'items': check, 'minItems': len(CHECK_IDS), 'maxItems': len(CHECK_IDS)},
        'results': {'type': 'array', 'items': QA_RESULT_SCHEMA, 'maxItems': 50},
        'layouts': {'type': 'object'},
        'fallbacks': {'type': 'array', 'items': _o({'page': _INT, 'from': _STR, 'to': _STR, 'why': _STR},
                                                   ('page', 'from', 'to', 'why'))},
        'critic': _o({'ran': _BOOL, 'why': _STR, 'edits': {'type': 'array', 'items': {'type': 'object'}},
                      'rolled_back': {'type': 'array', 'items': {'type': 'object'}}}, ('ran', 'why')),
        'thumbs': _INT,
        'phases': {'type': 'array', 'items': _o({'phase': {'type': 'string', 'enum': list(PHASES)}, 'calls': _INT,
                                                  'llm_in': _INT, 'llm_out': _INT, 'ms': _INT},
                                                 ('phase', 'calls', 'llm_in', 'llm_out', 'ms'))},
        'notes': {'type': 'array', 'items': _STR},
    }, ('version', 'score', 'preset', 'format', 'rounds', 'stop', 'checks', 'notes'))


# ---------- validation (a subset of JSON Schema: type, enum, properties, required, additionalProperties, items,
# minItems, maxItems, minimum, maximum, multipleOf, maxLength, pattern) ----------

_TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}


def _is(v: Any, t: str) -> bool:
    if t == 'integer':
        return isinstance(v, int) and not isinstance(v, bool)
    if t == 'number':
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(value: Any, schema: dict, where: str = '$') -> list[str]:
    """Problems with `value` under `schema`, as 'path: reason' strings; [] when it is valid."""
    import re
    out: list[str] = []
    types = schema.get('type')
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_is(value, t) for t in types):
            return [f'{where}: expected {"/".join(types)}']
    if 'enum' in schema and value not in schema['enum']:
        out.append(f'{where}: {str(value)[:40]!r} is not one of the allowed values')
    if isinstance(value, dict):
        props = schema.get('properties') or {}
        for k in schema.get('required') or ():
            if k not in value:
                out.append(f'{where}: missing {k!r}')
        for k, v in value.items():
            if k in props:
                out += validate(v, props[k], f'{where}.{k}')
            elif schema.get('additionalProperties') is False and 'properties' in schema:
                out.append(f'{where}: unexpected {k!r}')
    elif isinstance(value, list):
        if 'minItems' in schema and len(value) < schema['minItems']:
            out.append(f'{where}: at least {schema["minItems"]} items')
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            out.append(f'{where}: at most {schema["maxItems"]} items')
        if 'items' in schema:
            for i, v in enumerate(value):
                out += validate(v, schema['items'], f'{where}[{i}]')
    elif isinstance(value, str):
        if 'maxLength' in schema and len(value) > schema['maxLength']:
            out.append(f'{where}: longer than {schema["maxLength"]}')
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            out.append(f'{where}: does not match {schema["pattern"]}')
    elif _is(value, 'number'):
        if 'minimum' in schema and value < schema['minimum']:
            out.append(f'{where}: below {schema["minimum"]}')
        if 'maximum' in schema and value > schema['maximum']:
            out.append(f'{where}: above {schema["maximum"]}')
        if 'multipleOf' in schema and abs(value / schema['multipleOf'] - round(value / schema['multipleOf'])) > 1e-9:
            out.append(f'{where}: not a multiple of {schema["multipleOf"]}')
    return out


# ---------- new diagram kinds (builder V adds them to create/diagram.KINDS and the DocSpec) ----------

_LABEL = {'type': 'string', 'maxLength': 60}
_LABELS = {'type': 'array', 'items': _LABEL}


def _block(kind: str, props: dict, required: tuple) -> dict:
    return _o({'type': {'type': 'string', 'enum': [kind]}, 'title': _STR, **props}, ('type', *required))


DIAGRAM_BLOCK_SCHEMAS: dict[str, dict] = {
    'cycle': _block('cycle', {'steps': {**_LABELS, 'minItems': 3, 'maxItems': 8}}, ('steps',)),
    'venn': _block('venn', {'sets': {'type': 'array', 'minItems': 2, 'maxItems': 3,
                                     'items': _o({'label': _LABEL, 'items': _LABELS}, ('label',))},
                            'shared': _LABELS}, ('sets',)),
    'pyramid': _block('pyramid', {'levels': {**_LABELS, 'minItems': 3, 'maxItems': 6}}, ('levels',)),  # top first
    'matrix': _block('matrix', {'x_axis': _LABEL, 'y_axis': _LABEL,
                                'quadrants': {'type': 'array', 'minItems': 4, 'maxItems': 4,   # TL, TR, BL, BR
                                              'items': _o({'label': _LABEL, 'items': _LABELS}, ('label',))}},
                     ('quadrants',)),
    'mindmap': _block('mindmap', {'nodes': {'type': 'array', 'minItems': 2, 'maxItems': 30,  # like tree
                                            'items': _o({'id': _STR, 'label': _LABEL, 'parent': _NULL_STR},
                                                        ('id', 'label'))}}, ('nodes',)),
    'process': _block('process', {'steps': {'type': 'array', 'minItems': 2, 'maxItems': 7,
                                            'items': _o({'label': _LABEL, 'detail': {'type': 'string',
                                                                                     'maxLength': 120}},
                                                        ('label',))}}, ('steps',)),
    'comparison': _block('comparison', {'columns': {'type': 'array', 'minItems': 2, 'maxItems': 3,
                                                    'items': _o({'label': _LABEL, 'items': _LABELS}, ('label',))}},
                         ('columns',)),
    'labelled': _block('labelled', {'image': {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'},   # asset sha256
                                    'callouts': {'type': 'array', 'maxItems': 8, 'minItems': 1,
                                                 'items': _o({'label': _LABEL,
                                                              'x': {'type': 'number', 'minimum': 0, 'maximum': 1},
                                                              'y': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                                                             ('label', 'x', 'y'))}}, ('image', 'callouts')),
    'stat-cards': _block('stat-cards', {'stats': {'type': 'array', 'minItems': 2, 'maxItems': 4,
                                                  'items': _o({'value': {'type': ['string', 'number']},
                                                               'label': _LABEL, 'icon': _STR}, ('value', 'label'))}},
                         ('stats',)),
    'scatter': _block('scatter', {'x_label': _LABEL, 'y_label': _LABEL,
                                  'points': {'type': 'array', 'minItems': 2, 'maxItems': 200,
                                             'items': _o({'x': _NUM, 'y': _NUM, 'label': _LABEL}, ('x', 'y'))}},
                      ('points',)),
}
DIAGRAM_KINDS_NEW = tuple(DIAGRAM_BLOCK_SCHEMAS)
