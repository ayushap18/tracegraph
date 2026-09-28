"""Design files (docs/PLAN-files-robust.md section 4): a design system in Markdown, CSS or JSON tokens read by code, at 0
tokens, into colours by role, fonts and a corner radius. The model never sees the design file; the renderers get the
result as `spec['design']` and turn it into a theme (themes.from_design).

Roles come only from the label of a line ("Page background: Cream (#f7f4ed)") or from a description that names one
("**Cream** (#f7f4ed): Page background"). Words in a colour's own name ("Muted Gray", "Slate") never vote. Lines that
say what not to do ("Don't use pure white") and focus-ring colours cast no vote and never reach the chart palette.
"""
from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass, field

ROLES = ('bg', 'surface', 'text', 'heading', 'muted', 'accent', 'border', 'header_bg', 'header_text', 'stripe',
         'code_bg')
TEXT_ROLES = {'text', 'muted', 'heading', 'header_text'}
GENERIC_FONTS = {'serif', 'sans-serif', 'sans', 'monospace', 'mono', 'ui-sans-serif', 'ui-serif', 'ui-monospace',
                 'system-ui', 'ui-rounded', 'cursive', 'fantasy', '-apple-system', 'blinkmacsystemfont', 'emoji',
                 'math', 'fangsong', 'inherit', 'initial'}
QUICK_WEIGHT = 5

# the request asks to use a design file
MENTION = re.compile(
    r'\b(?:design(?:[\s_-]?system)?|style[\s_-]?guide|brand(?:[\s_-]?(?:guide|book|kit))?|theme|tokens)\.'
    r'(?:md|json|css|txt)\b'
    r'|\b(?:use|follow|apply|match|with|using|in)\s+(?:the|my|our|this|attached)\s+'
    r'(?:(?:attached|uploaded|provided|included|shared)\s+)?(?:design(?:\s+(?:system|file|guide|tokens))?|'
    r'style\s*guide|brand(?:\s+(?:guide|book|kit))?)\b'
    r'|\b(?:attached|uploaded)\s+(?:design|style\s*guide|brand)\b'
    r'|\bfor the (?:design|style|look)\b', re.I)
# a design file asked for without a name ("follow our style guide"); "for the design team" is not one
ASKS_FILE = re.compile(r'\b(?:use|follow|apply|match|with|using)\s+(?:the|my|our|this|attached)\s+'
                       r'(?:(?:attached|uploaded|provided|included|shared)\s+)?(?:design\s+(?:file|system|guide|tokens)|'
                       r'style\s*guide|brand\s+(?:guide|book|kit))\b'
                       r'|\b(?:attached|uploaded)\s+(?:design\s+(?:file|system|guide|tokens)|style\s*guide|'
                       r'brand\s+(?:guide|book|kit))\b', re.I)
NAMED_FILE = re.compile(r'\b[\w-]*(?:design|style|brand|theme|tokens)[\w-]*\.(?:md|json|css|txt)\b', re.I)
ABOUT_FILE = re.compile(r'\b(?:summari[sz]e|explain|describe|review|critique|audit|compare|what\s+(?:is|does))\b'
                        r'(?:\W+\w+){0,6}?\W+(?:design|style|brand)|\b(?:design|style|brand)\w*(?:\W+\w+){0,4}?\W+'
                        r'(?:summari[sz]e|explain|describe|review|critique|audit|compare)', re.I)
DESIGN_NAME = re.compile(r'design|style|brand|theme|tokens', re.I)
DESIGN_HEADING = re.compile(r'^\s{0,3}#{1,6}\s*(?:\d+[.)]\s*)?.*\b(?:colou?rs?|palette|typography|spacing|components?)\b',
                            re.I | re.M)

# ---------- colours ----------

HEX = r'#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b'
RGB = r'rgba?\(\s*\d{1,3}%?\s*[, ]\s*\d{1,3}%?\s*[, ]\s*\d{1,3}%?\s*(?:[,/]\s*[\d.]+%?\s*)?\)'
HSL = r'hsla?\(\s*[\d.]+(?:deg)?\s*[, ]\s*[\d.]+%\s*[, ]\s*[\d.]+%\s*(?:[,/]\s*[\d.]+%?\s*)?\)'
OKLCH = r'oklch\(\s*[\d.]+%?\s+[\d.]+%?\s+[\d.]+(?:deg)?\s*(?:/\s*[\d.]+%?\s*)?\)'
COLOR = re.compile(f'{HEX}|{RGB}|{HSL}|{OKLCH}', re.I)
# a CSS custom property whose value is a bare HSL triplet (shadcn/ui, Lovable): --background: 0 0% 100%;
BARE_HSL = re.compile(r'^(\s*--[\w-]+\s*:\s*)(-?[\d.]+)(?:deg)?\s+([\d.]+)%\s+([\d.]+)%\s*(?:/\s*([\d.]+%?))?\s*;?\s*$')


def _bare_hsl(line: str) -> str:
    m = BARE_HSL.match(line)
    if not m:
        return line
    alpha = f', {m.group(5)}' if m.group(5) else ''
    return f'{m.group(1)}hsl({m.group(2)}, {m.group(3)}%, {m.group(4)}%{alpha});'


def _oklch_rgb(lig: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """OKLCH to sRGB channels 0..255 (clamped to the sRGB gamut)."""
    import math
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l_ = (lig + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lig - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lig - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)

    def gamma(x):
        x = max(0.0, min(1.0, x))
        return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return tuple(gamma(x) * 255 for x in lin)


def _clamp(v, lo=0, hi=255):
    return max(lo, min(hi, v))


def parse_color(s: str) -> tuple[str, float] | None:
    """('RRGGBB', alpha) from a CSS colour value, or None."""
    s = s.strip()
    try:
        if s.startswith('#'):
            h = s[1:]
            if len(h) in (3, 4):
                h = ''.join(c * 2 for c in h)
            alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return h[:6].upper(), alpha
        nums = [x for x in re.findall(r'[\d.]+%?', s)]
        if s.lower().startswith('rgb'):
            ch = [round(float(x[:-1]) * 2.55) if x.endswith('%') else int(float(x)) for x in nums[:3]]
            alpha = nums[3] if len(nums) > 3 else '1'
            a = float(alpha[:-1]) / 100 if alpha.endswith('%') else float(alpha)
            return ''.join(f'{_clamp(c):02X}' for c in ch), max(0.0, min(1.0, a))
        if s.lower().startswith('oklch'):
            lig = float(nums[0][:-1]) / 100 if nums[0].endswith('%') else float(nums[0])
            chroma = float(nums[1][:-1]) * 0.004 if nums[1].endswith('%') else float(nums[1])
            alpha = nums[3] if len(nums) > 3 else '1'
            a = float(alpha[:-1]) / 100 if alpha.endswith('%') else float(alpha)
            return hex_of(_oklch_rgb(lig, chroma, float(nums[2]))), max(0.0, min(1.0, a))
        if s.lower().startswith('hsl'):
            h, sat, lig = float(nums[0]) / 360, float(nums[1].rstrip('%')) / 100, float(nums[2].rstrip('%')) / 100
            r, g, b = colorsys.hls_to_rgb(h % 1, lig, sat)
            alpha = nums[3] if len(nums) > 3 else '1'
            a = float(alpha[:-1]) / 100 if alpha.endswith('%') else float(alpha)
            return ''.join(f'{_clamp(round(c * 255)):02X}' for c in (r, g, b)), max(0.0, min(1.0, a))
    except (ValueError, IndexError):
        return None
    return None


def rgb_of(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def hex_of(rgb) -> str:
    return ''.join(f'{_clamp(round(c)):02X}' for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """a moved t of the way to b."""
    ra, rb = rgb_of(a), rgb_of(b)
    return hex_of(x + (y - x) * t for x, y in zip(ra, rb))


def over(fg: str, alpha: float, bg: str) -> str:
    """fg at `alpha` composited over bg."""
    return mix(bg, fg, alpha)


def luminance(h: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb_of(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def saturation(h: str) -> float:
    r, g, b = (c / 255 for c in rgb_of(h))
    _, lig, sat = colorsys.rgb_to_hls(r, g, b)
    return sat if 0.12 < lig < 0.9 else 0.0


# ---------- the parsed result ----------


@dataclass
class FontSpec:
    family: str
    fallbacks: list[str]
    category: str          # 'sans' | 'serif' | 'mono'

    def to_dict(self) -> dict:
        return {'family': self.family, 'fallbacks': list(self.fallbacks), 'category': self.category}


@dataclass
class DesignTokens:
    name: str
    colors: dict[str, str]
    palette: list[str]
    hatch: bool
    heading_font: FontSpec | None
    body_font: FontSpec | None
    mono_font: FontSpec | None
    radius: int | None
    confidence: float
    notes: list[str] = field(default_factory=list)


# role -> phrases, tried in this order; a matched span is consumed so "secondary text" never also votes text
ROLE_WORDS = [
    ('muted', r'secondary(?:\s+text)?|tertiary|muted|captions?|subtle|placeholders?|hints?'),
    ('heading', r'headings?|headlines?|titles?|display|h[1-6]'),
    ('surface', r'(?:primary\s+|secondary\s+)?surfaces?|cards?|panels?|elevated|modals?|popovers?'),
    ('text', r'(?:primary\s+|main\s+|body\s+|default\s+)?text(?:\s+(?:primary|default|base|main|body))?|'
             r'body(?:\s+copy)?|copy|foreground|fg|ink\s+text'),
    ('bg', r'(?:page\s+|app\s+|main\s+)?(?:background|backdrop)s?|canvas|page|base|bg'),
    ('border', r'borders?|dividers?|rules?|strokes?|outlines?|hairlines?|separators?'),
    ('accent', r'accents?|primary|brand|links?|cta|highlights?'),
]
ROLE_RX = [(role, re.compile(rf'\b(?:{rx})\b', re.I)) for role, rx in ROLE_WORDS]
BUTTON = re.compile(r'\b(?:buttons?|cta|badges?|chips?|pills?)\b', re.I)
ON_DARK = re.compile(r'\bon[\s-](?:dark|primary|accent|brand|buttons?)\b|\binverse\b|\binverted\b', re.I)
ON_FILL = re.compile(r'\b(?:primary|secondary|accent|destructive|brand)\s+(?:foreground|fg)\b', re.I)
SURFACE_FG = re.compile(r'\b(?:card|popover|surface|sidebar)\s+(?:foreground|fg)\b', re.I)
# shadcn-style fill tokens (--muted, --accent, --secondary with a matching --X-foreground) are hover and chip fills, not
# muted text or the brand accent
FILL_TOKEN = re.compile(r'^\s*--(muted|accent|secondary)\s*:', re.I)
FONT_LABEL = re.compile(r'\b(?:font|fonts|family|typeface)\b', re.I)
NOT_FONT = re.compile(r'\b(?:size|sizes|weight|weights|style|height|spacing|line|scale|leading|tracking|colou?r|variant|'
                      r'feature|features|smoothing|stretch)\b', re.I)
BTN_TEXT = re.compile(r'\b(?:text|labels?|foreground|fg|copy)\b', re.I)
EXCLUDE_LINE = re.compile(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*(?:don\'?t|do\s+not|avoid|never)\b', re.I)
FOCUS_ONLY = re.compile(r'\bfocus(?:\s+(?:rings?|states?|outlines?|indicators?))?\s+only\b|\bonly\s+for\s+focus\b|'
                        r'\bfocus\s+rings?\b', re.I)
QUICK = re.compile(r'\bquick\s+(?:colou?r\s+)?reference\b|\bsummary\b|\bcheat\s*sheet\b', re.I)
TYPO_HEAD = re.compile(r'\btypograph|\bfonts?\b|\btype\s+scale\b', re.I)
MD_HEAD = re.compile(r'^\s{0,3}(#{1,6})\s+(.*)$')


def _roles_in(text: str) -> list[str]:
    """Role words in a label or description, most specific first, each span used once."""
    t = ' '.join(re.sub(r'[`*_#>\[\](){}"\'|]', ' ', text).replace('-', ' ').split()).lower()
    if not t:
        return []
    if ON_FILL.search(t):  # shadcn: --primary-foreground is text on a primary fill
        return ['header_text']
    if SURFACE_FG.search(t):  # --card-foreground is body text on a card
        return ['text']
    if BUTTON.search(t) or ON_DARK.search(t):
        if ON_DARK.search(t) or BTN_TEXT.search(t):
            return ['header_text']
        return ['header_bg']
    out = []
    for role, rx in ROLE_RX:
        if rx.search(t):
            if not (role == 'text' and 'muted' in out):  # "text muted" is one role
                out.append(role)
            t = rx.sub(' ', t)
    return out


def _split_label(line: str) -> tuple[str, str]:
    """(label, description): the text before the first colon that is not inside a colour value, and after."""
    masked = COLOR.sub(lambda m: 'x' * len(m.group(0)), line)
    i = masked.find(':')
    if i < 0:
        return '', line
    return line[:i], line[i + 1:]


def _mentions(line: str) -> list[tuple[str, float, list[str]]]:
    """[(RRGGBB, alpha, roles)] for each colour on one line."""
    found = list(COLOR.finditer(line))
    if not found:
        return []
    label, desc = _split_label(line)
    cut = len(label) if desc is not line else -1
    many = len(found) > 1
    out = []
    for m in found:
        c = parse_color(m.group(0))
        if c is None:
            continue
        if cut < 0 and many:  # no label, several colours: the sentence each one is in
            roles = _roles_in(_sentence(line, m))
        elif cut < 0:  # no label: the whole line
            roles = _roles_in(line)
        elif m.start() < cut:  # the colour sits in the label: the label is its name, the description names the role
            roles = _roles_in(desc)
        else:  # the colour is in the description: the label names the role, plus the words right after the colour
            after = _segment_after(line, m, found)
            roles = _roles_in(f'{label} {after}') if many else (_roles_in(label) or _roles_in(after))
        out.append((c[0], c[1], roles))
    return out


def _sentence(line: str, m: re.Match) -> str:
    """The sentence of a line a colour value sits in (colour values masked so their dots never end one)."""
    masked = COLOR.sub(lambda x: 'x' * len(x.group(0)), line)
    ends = [e.end() for e in re.finditer(r'[.;!?](?:\s|$)', masked)]
    start = max([e for e in ends if e <= m.start()], default=0)
    end = min([e for e in ends if e >= m.end()], default=len(line))
    return line[start:end]


def _segment_after(line: str, m: re.Match, found: list) -> str:
    """The words after a colour value up to the next comma, semicolon or colour."""
    end = len(line)
    for f in found:
        if f.start() > m.end():
            end = f.start()
            break
    seg = line[m.end():end]
    seg = re.split(r'[,;.]', seg, maxsplit=1)[0]
    return seg


def _json_lines(text: str) -> list[str] | None:
    """A JSON token file flattened to 'path.to.key: value' lines."""
    s = text.strip()
    if not s.startswith(('{', '[')):
        return None
    try:
        data = json.loads(s)
    except (ValueError, RecursionError):
        return None
    out = []

    def walk(v, path):
        if len(out) > 5000:
            return
        if isinstance(v, dict):
            for vk in ('value', '$value'):
                if vk in v and not isinstance(v[vk], (dict, list)):
                    out.append(f'{" ".join(path)}: {v[vk]}')
                    return
            for k, x in v.items():
                walk(x, [*path, str(k)])
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, path)
        elif isinstance(v, (str, int, float)):
            out.append(f'{" ".join(path)}: {v}')
    walk(data, [])
    return out


def _font_list(value: str) -> list[str]:
    value = value.split(';')[0].replace('{', ' ').replace('}', ' ')
    value = re.sub(r'\bfallbacks?\s*:?', ',', value, flags=re.I)
    names = []
    for part in re.split(r',', value):
        p = part.strip().strip('`"\' ;').strip()
        if not p:
            continue
        if re.search(r'\b(?:weight|size|line|px|rem|em)\b|\d{3}', p, re.I) and names:
            break
        p = re.sub(r'\s*\((?:[^)]*)\)\s*', ' ', p).strip()
        p = re.split(r'\s+(?:at|with|for|in|weight)\b', p, maxsplit=1, flags=re.I)[0].strip()
        if p and len(p) <= 60:
            names.append(p)
    return names


def _category(names: list[str], label: str = '') -> str:
    low = [n.lower() for n in names]
    if any(n in ('monospace', 'ui-monospace', 'mono') or 'mono' in n or n in ('menlo', 'consolas', 'courier new')
           for n in low) or re.search(r'\b(?:code|mono)\b', label, re.I):
        return 'mono'
    if any(n in ('serif', 'ui-serif') for n in low):
        return 'serif'
    if any(n in ('sans-serif', 'ui-sans-serif', 'system-ui', 'sans') for n in low):
        return 'sans'
    if any(re.search(r'serif|georgia|times|garamond|cambria|merriweather|playfair|lora\b|fraunces|baskerville|'
                     r'crimson|caslon|bodoni|didot|spectral|literata|domine|cormorant|charter|palatino|minion',
                     n) and 'sans' not in n for n in low):
        return 'serif'
    return 'sans'


def _font_spec(names: list[str], label: str = '', inherit: FontSpec | None = None) -> FontSpec | None:
    if not names:
        return None
    family = next((n for n in names if n.lower() not in GENERIC_FONTS), names[0])
    rest = [n for n in names if n != family]
    if not rest and inherit is not None and inherit.family.lower() == family.lower():
        rest = list(inherit.fallbacks)
    named = _category(names, label)
    cat = named if rest or family.lower() in GENERIC_FONTS or not inherit or named != 'sans' else inherit.category
    return FontSpec(family, rest, cat)


def parse_design(text: str, name: str = 'design.md') -> DesignTokens | None:
    """A design file's colours by role, fonts and radius, or None when it names no background or text colour in a way
    that can be read."""
    text = str(text or '')[:400_000]
    lines = _json_lines(text) or [_bare_hsl(y) for x in text.replace('\r\n', '\n').split('\n')
                                  for y in re.split(r'[;{}](?=\s*--[\w-]+\s*:)', x)]
    fills = {m.lower() for m in re.findall(r'--(muted|accent|secondary)-foreground\s*:', text, re.I)}
    votes: dict[str, dict[str, float]] = {}
    alpha_votes: list[tuple[str, float, str, float]] = []   # (hex, alpha, role, weight) composited later
    order: dict[str, int] = {}
    excluded: set[str] = set()
    textual: set[str] = set()
    fonts: dict[str, list[str]] = {}
    font_labels: dict[str, str] = {}
    radius = None
    heading_stack: list[tuple[int, str]] = []

    def section_is(rx) -> bool:
        return any(rx.search(h) for _, h in heading_stack)

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        h = MD_HEAD.match(line)
        if h:
            level = len(h.group(1))
            heading_stack = [x for x in heading_stack if x[0] < level] + [(level, h.group(2))]
            continue
        weight = QUICK_WEIGHT if section_is(QUICK) else 1
        if radius is None:
            r = re.search(r'\b(?:border[\s-]?radius|radius|rounded(?:\s+corners?)?)\b[^0-9\n]{0,24}(\d{1,3}(?:\.\d+)?)\s*'
                          r'(px|rem)\b', line, re.I)
            if r:
                px = float(r.group(1)) * (16 if r.group(2).lower() == 'rem' else 1)
                radius = max(0, min(24, round(px)))
        # fonts: font-family lines anywhere, labelled lines under a typography heading
        label, desc = _split_label(line)
        if not label and section_is(TYPO_HEAD) and line.startswith('|'):
            # a Markdown table row under Typography: | Headings | Playfair Display |
            cells = [c.strip() for c in line.strip('|').split('|')]
            cells = [c for c in cells if c]
            if len(cells) >= 2 and not all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                label, desc = cells[0], cells[1]
        lab = re.sub(r'[`*_\-.$]', ' ', label).replace(' value', ' ').strip().lower()
        font_line = bool(label and FONT_LABEL.search(lab) and not NOT_FONT.search(lab))
        if 'font-family' in line.lower() or ((section_is(TYPO_HEAD) or font_line) and label and
                                             not COLOR.search(line)):
            value = desc if label else line
            kind = ('mono' if re.search(r'\b(?:code|mono|monospace)\b', lab) else
                    'heading' if re.search(r'\b(?:headings?|display|titles?|headlines?|h[1-6])\b', lab) else
                    'body' if re.search(r'\b(?:font|family|body|text|base|primary|sans|serif|typeface)\b', lab) or
                    'font-family' in lab else None)
            if kind and kind not in fonts:
                names = _font_list(value)
                if names and not all(re.fullmatch(r'[\d.]+\w*', n) for n in names):
                    fonts[kind], font_labels[kind] = names, lab
        # colours
        mentions = _mentions(line)
        if not mentions:
            continue
        skip = bool(EXCLUDE_LINE.match(line) or FOCUS_ONLY.search(line))
        fill = FILL_TOKEN.match(line)
        if fill and fill.group(1).lower() in fills:
            continue
        for hexv, alpha, roles in mentions:
            if skip:
                excluded.add(hexv)
                continue
            order.setdefault(hexv, len(order))
            for role in roles:
                if role in TEXT_ROLES:
                    textual.add(hexv)
                if alpha < 0.999:
                    alpha_votes.append((hexv, alpha, role, weight))
                else:
                    votes.setdefault(role, {})
                    votes[role][hexv] = votes[role].get(hexv, 0) + weight

    def top(role: str) -> str | None:
        v = votes.get(role)
        if not v:
            return None
        return max(v, key=lambda k: (v[k], -order.get(k, 0)))

    colors: dict[str, str] = {}
    notes: list[str] = []
    bg = top('bg')
    if bg is None and alpha_votes:
        bg = next((over(h, a, 'FFFFFF') for h, a, r, _ in alpha_votes if r == 'bg'), None)
    if bg:
        colors['bg'] = bg
    base = bg or 'FFFFFF'
    for hexv, alpha, role, weight in alpha_votes:
        if role == 'bg' and bg:
            continue
        solid = over(hexv, alpha, base)
        order.setdefault(solid, order.get(hexv, len(order)))
        votes.setdefault(role, {})
        votes[role][solid] = votes[role].get(solid, 0) + weight
        if role in TEXT_ROLES:
            textual.add(solid)
    # text: when candidates disagree, the one that reads best on the background wins
    cands = [c for c in (votes.get('text') or {}) if c != base]
    if cands:
        best = max(cands, key=lambda c: (round(contrast(c, base), 3), votes['text'][c]))
        colors['text'] = best
        if len(cands) > 1:
            loser = max((c for c in cands if c != best), key=lambda c: (votes['text'][c], -order.get(c, 0)))
            muted_top = top('muted')
            if muted_top is None and contrast(loser, base) >= 4.5:
                colors['muted'] = loser
                muted_top = loser
            count = {2: 'two', 3: 'three', 4: 'four'}.get(len(cands), str(len(cands)))
            if muted_top == loser:
                notes.append(f'The design gives {count} body text colours; used #{best} and kept #{loser} for '
                             f'secondary text.')
            else:
                notes.append(f'The design gives {count} body text colours; used #{best}, the one that reads best on '
                             f'the background.')
    for role in ('surface', 'heading', 'muted', 'accent', 'border', 'header_bg', 'header_text'):
        if role not in colors and (c := top(role)):
            colors[role] = c
    # palette: saturated colours that are not text colours and not excluded, in order of appearance
    seen = sorted(order, key=lambda k: order[k])
    palette = [c for c in seen if c not in excluded and c not in textual and saturation(c) >= 0.3
               and c != colors.get('bg')][:6]
    hatch = len(palette) < 3
    if hatch:
        t = colors.get('text') or '1F2328'
        palette = list(dict.fromkeys([t, colors.get('muted') or mix(t, base, 0.35), mix(t, base, 0.55),
                                      mix(t, base, 0.75)]))
    body = _font_spec(fonts.get('body', []), font_labels.get('body', ''))
    heading = _font_spec(fonts.get('heading', []), font_labels.get('heading', ''), inherit=body)
    if heading is not None and body is not None and heading.family.lower() == body.family.lower() and \
            not heading.fallbacks:
        heading = FontSpec(heading.family, list(body.fallbacks), body.category)
    mono = _font_spec(fonts.get('mono', []), 'code')
    if mono is not None:
        mono.category = 'mono'
    found = sum(1 for r in ('bg', 'text', 'heading') if r in colors)
    confidence = round(found / 3 * 0.8 + (0.2 if body or heading else 0.0), 3)
    if confidence < 0.34 or not ('bg' in colors or 'text' in colors):
        return None
    return DesignTokens(name=str(name or 'design.md')[:120], colors=colors, palette=palette, hatch=hatch,
                        heading_font=heading, body_font=body, mono_font=mono, radius=radius, confidence=confidence,
                        notes=notes)


def to_spec(tokens: DesignTokens) -> dict:
    """The spec['design'] JSON."""
    return {'name': tokens.name, 'colors': dict(tokens.colors), 'palette': list(tokens.palette), 'hatch': tokens.hatch,
            'heading_font': tokens.heading_font.to_dict() if tokens.heading_font else None,
            'body_font': tokens.body_font.to_dict() if tokens.body_font else None,
            'mono_font': tokens.mono_font.to_dict() if tokens.mono_font else None,
            'radius': tokens.radius, 'notes': list(tokens.notes), 'confidence': tokens.confidence}


_HEX6 = re.compile(r'^[0-9A-F]{6}$')


def _hex(v) -> str | None:
    s = str(v or '').strip().lstrip('#').upper()
    if re.fullmatch(r'[0-9A-F]{3}', s):
        s = ''.join(c * 2 for c in s)
    return s if _HEX6.match(s) else None


def _clean_font(f) -> dict | None:
    if not isinstance(f, dict):
        return None
    fam = re.sub(r'[\x00-\x1f<>]', '', str(f.get('family') or '')).strip()[:60]
    if not fam:
        return None
    fb = [re.sub(r'[\x00-\x1f<>]', '', str(x)).strip()[:60] for x in f.get('fallbacks') or [] if isinstance(x, str)]
    cat = f.get('category') if f.get('category') in ('sans', 'serif', 'mono') else 'sans'
    return {'family': fam, 'fallbacks': [x for x in fb if x][:8], 'category': cat}


def clean_design(d) -> dict | None:
    """A spec['design'] value made safe (colours RRGGBB, known roles, short strings), or None when it is not a design."""
    if not isinstance(d, dict):
        return None
    raw = d.get('colors') if isinstance(d.get('colors'), dict) else {}
    colors = {r: h for r in ROLES if (h := _hex(raw.get(r)))}
    if not colors:
        return None
    palette = [h for x in (d.get('palette') or []) if isinstance(d.get('palette'), list) and (h := _hex(x))][:6]
    try:
        radius = None if d.get('radius') is None else max(0, min(24, int(float(d.get('radius')))))
    except (TypeError, ValueError, OverflowError):
        radius = None
    try:
        conf = max(0.0, min(1.0, float(d.get('confidence', 1.0))))
    except (TypeError, ValueError):
        conf = 1.0
    notes = [re.sub(r'[\x00-\x1f]', ' ', str(n))[:300] for n in (d.get('notes') or []) if isinstance(n, str)][:10] \
        if isinstance(d.get('notes'), list) else []
    return {'name': re.sub(r'[\x00-\x1f<>]', '', str(d.get('name') or 'design.md'))[:120] or 'design.md',
            'colors': colors, 'palette': palette, 'hatch': bool(d.get('hatch', len(palette) < 3)),
            'heading_font': _clean_font(d.get('heading_font')), 'body_font': _clean_font(d.get('body_font')),
            'mono_font': _clean_font(d.get('mono_font')), 'radius': radius, 'notes': notes, 'confidence': conf}


# ---------- detection ----------


def is_design(meta: dict, text: str, request: str = '') -> float:
    """How sure it is that an attached file is a design system (0..1); 0.5 or more is a design."""
    name = str((meta or {}).get('name') or '')
    text = str(text or '')[:200_000]
    score = 0.0
    if DESIGN_NAME.search(name):
        score += 0.4
    heads = {m.group(0).strip().lower() for m in DESIGN_HEADING.finditer(text)}
    score += min(0.3, 0.1 * len(heads))
    n_colors = len(COLOR.findall(text))
    if n_colors >= 5:
        score += 0.2
    roles = 0
    for line in text.split('\n')[:3000]:
        for _, _, r in _mentions(line):
            if r:
                roles += 1
        if roles >= 2:
            break
    if roles >= 2:
        score += 0.2
    req = str(request or '')
    stem = re.sub(r'\.\w+$', '', name).lower()
    if req and (re.search(r'\bthe\s+design\s+file\b', req, re.I) or (name and name.lower() in req.lower()) or
                (stem and len(stem) >= 4 and re.search(rf'\b{re.escape(stem)}\b', req.lower())) or
                (MENTION.search(req) and DESIGN_NAME.search(name))):
        score += 0.5
    elif req and MENTION.search(req) and score >= 0.3:
        score += 0.3
    return round(min(1.0, score), 3)


def split_docs(docs, request) -> tuple[list, list]:
    """(design docs, content docs) of attached (meta, text) pairs."""
    design, content = [], []
    req = str(request or '')
    mention = bool(MENTION.search(req))
    about = bool(ABOUT_FILE.search(req))
    for meta, text in docs or []:
        try:
            score = is_design(meta, text, req)
        except Exception:
            score = 0.0
        if score >= 0.5 and not about and (mention or score >= 0.7):
            design.append((meta, text))
        else:
            content.append((meta, text))
    return design, content


def mentioned_name(request: str) -> str | None:
    """The design file a request names ("design.md"), or 'the design file' when it asks for one without a name."""
    m = NAMED_FILE.search(str(request or ''))
    if m:
        return m.group(0)
    return 'the design file' if ASKS_FILE.search(str(request or '')) else None


def unreadable_note(name: str) -> str:
    return (f'The design file {name} did not name its colours or fonts in a way that could be read, so the standard '
            f'style was used.')


def missing_note(name: str) -> str:
    return (f'You mentioned {name}, but no design file is attached to this message. Attach it and convert the file to '
            f'apply it (0 tokens).')


def applied(design: dict, theme: dict, fonts_used: dict) -> dict:
    """The CreatedFile.design (DesignApplied) dict: what the design asked for and what the file really uses."""
    colors = {r: theme[r] for r in ROLES if isinstance(theme.get(r), str) and _HEX6.match(theme[r])}
    hf, bf = design.get('heading_font') or {}, design.get('body_font') or {}
    notes = list(dict.fromkeys([*(design.get('notes') or []), *(theme.get('design_notes') or []),
                                *(fonts_used.get('notes') or [])]))
    return {'name': design.get('name') or 'design.md', 'colors': colors, 'palette': list(theme.get('palette') or []),
            'heading_font': hf.get('family'), 'body_font': bf.get('family'),
            'fonts_used': {'heading': fonts_used.get('heading'), 'body': fonts_used.get('body')},
            'nudged': list(theme.get('nudged') or []), 'notes': notes,
            'confidence': float(design.get('confidence', 1.0))}
