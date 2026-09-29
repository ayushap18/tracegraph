"""Design tokens (docs/PLAN-designer.md 3.2 and 9.3): one DesignSystem per file, built in priority order from an attached
design.md (create/design.py), explicit prompt words (presets.read_prompt), the chosen preset, then defaults.

Owner: builder F. The dataclasses are contract (fields are fixed).
"""
from __future__ import annotations

import colorsys
import copy
import re
from dataclasses import asdict, dataclass, field

from .plan import TOKEN_COLORS, TYPE_STEPS

# colour roles: create/design.py ROLES plus accent2 (second accent), overlay (tint behind text on photos) and on_accent
# (text on an accent fill). Values are RRGGBB without '#'.
COLOR_ROLES = ('bg', 'surface', 'text', 'heading', 'muted', 'accent', 'accent2', 'border', 'header_bg', 'header_text',
               'stripe', 'code_bg', 'overlay', 'on_accent')
FONT_ROLES = ('display', 'heading', 'body', 'caption', 'mono')
IMAGE_TREATMENTS = ('full-bleed', 'rounded', 'framed', 'duotone')
ACCENT_SHAPES = ('bar', 'circle', 'blob', 'corner', 'underline', 'none')
# a modular scale: size(step) = base * ratio ** STEP_POWER[step]
STEP_POWER = {'caption': -1, 'body': 0, 'lead': 1, 'h3': 2, 'h2': 3, 'h1': 4, 'display': 5}
BASE_PT = {'pptx': 18.0, 'pdf': 10.5, 'docx': 11.0, 'md': 11.0, 'xlsx': 11.0}


@dataclass
class TypeScale:
    ratio: float = 1.25              # 1.25..1.5 by preset
    line_height: float = 1.25        # body leading multiple
    display_line_height: float = 1.05
    display_tracking: float = -0.01  # em, display and h1 only


@dataclass
class Families:
    display: str = 'Inter'
    heading: str = 'Inter'
    body: str = 'Inter'
    caption: str = 'Inter'
    mono: str = 'JetBrains Mono'


@dataclass
class Spacing:
    unit: float = 8.0                # the 8-point grid
    columns: int = 12
    slide_margin: float = 48.0       # points, 960x540 slides
    page_margin: float = 56.7        # points (2 cm), print pages
    gutter: float = 16.0             # points between columns
    rows_slide: int = 12             # layout grid rows on a slide
    rows_page: int = 16              # layout grid rows on a print page


@dataclass
class Shape:
    radius: float = 8.0              # points
    stroke: float = 1.0              # points
    shadow: bool = False
    accent_shape: str = 'bar'        # ACCENT_SHAPES


@dataclass
class MinSizes:
    slide_body: float = 18.0
    slide_caption: float = 12.0
    print_body: float = 10.0
    print_caption: float = 8.0


@dataclass
class DesignSystem:
    id: str                                          # preset id, or 'custom'
    name: str
    dark: bool
    colors: dict[str, str]                           # COLOR_ROLES -> RRGGBB
    chart_palette: list[str]                         # 6 colour-blind-safe RRGGBB
    gradients: list[tuple[str, str]] = field(default_factory=list)   # (from, to) RRGGBB pairs
    overlay_alpha: float = 0.55                      # overlay opacity behind text on photos
    families: Families = field(default_factory=Families)
    scale: TypeScale = field(default_factory=TypeScale)
    spacing: Spacing = field(default_factory=Spacing)
    shape: Shape = field(default_factory=Shape)
    image: str = 'full-bleed'                        # IMAGE_TREATMENTS
    min_sizes: MinSizes = field(default_factory=MinSizes)
    hatch: bool = False                              # charts by pattern, not colour (mono)
    grey_images: bool = False
    justify: bool = False                            # never True for high-legibility
    do: list[str] = field(default_factory=list)      # design.md v2 "do" lines
    dont: list[str] = field(default_factory=list)    # design.md v2 "don't" lines
    source: list[str] = field(default_factory=list)  # provenance in priority order, e.g. ['design.md', 'prompt:dark', 'preset:minimal']
    notes: list[str] = field(default_factory=list)   # honest sentences (a colour nudged for contrast, a font swapped)

    def size(self, step: str, fmt: str) -> float:
        """Points for a type step in a format, never below the format's minimum body/caption size."""
        base = BASE_PT.get(fmt, 11.0)
        pt = round(base * self.scale.ratio ** STEP_POWER[step], 1)
        slide = fmt == 'pptx'
        floor = (self.min_sizes.slide_caption if slide else self.min_sizes.print_caption) if step == 'caption' else \
            (self.min_sizes.slide_body if slide else self.min_sizes.print_body)
        return max(pt, floor)

    def color(self, name: str) -> str:
        """A token colour name (plan.TOKEN_COLORS) as RRGGBB; chart1..chart6 index the chart palette."""
        if name.startswith('chart'):
            pal = self.chart_palette or [self.colors['accent']]
            return pal[(int(name[5:]) - 1) % len(pal)]
        return self.colors[name]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'DesignSystem':
        d = dict(d)
        d['families'] = Families(**(d.get('families') or {}))
        d['scale'] = TypeScale(**(d.get('scale') or {}))
        d['spacing'] = Spacing(**(d.get('spacing') or {}))
        d['shape'] = Shape(**(d.get('shape') or {}))
        d['min_sizes'] = MinSizes(**(d.get('min_sizes') or {}))
        d['gradients'] = [tuple(g) for g in d.get('gradients') or []]
        return cls(**d)


assert set(STEP_POWER) == set(TYPE_STEPS)
assert set(COLOR_ROLES) | {f'chart{i}' for i in range(1, 7)} == set(TOKEN_COLORS)


# ---------- colour helpers and contrast repair (A4) ----------

HEX6 = re.compile(r'^[0-9A-F]{6}$')
# text colour -> the backgrounds it is drawn on and the ratio it must reach there. Text on an overlay (a photo behind
# a tint) uses `text`; repair() raises overlay_alpha until that reads against the worst photo pixel.
TEXT_RULES = (('text', ('bg', 'surface', 'stripe', 'code_bg'), 4.5), ('heading', ('bg', 'surface'), 4.5),
              ('muted', ('bg', 'surface'), 4.5), ('accent', ('bg',), 4.5), ('header_text', ('header_bg',), 4.5),
              ('accent2', ('bg',), 3.0))
ROLE_LABEL = {'text': 'body text colour', 'heading': 'heading colour', 'muted': 'muted text colour',
              'accent': 'accent colour', 'header_text': 'table header text colour', 'accent2': 'second accent colour',
              'on_accent': 'colour for text on the accent', 'palette': 'chart colours'}
# derived from the background when a design sets bg but not these
BG_DERIVED = ('surface', 'stripe', 'code_bg', 'border', 'overlay')


def _hex(v) -> str | None:
    s = str(v or '').strip().lstrip('#').upper()
    if re.fullmatch(r'[0-9A-F]{3}', s):
        s = ''.join(c * 2 for c in s)
    return s if HEX6.match(s) else None


def _th():
    from ..create import themes
    return themes


def _hls(h: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def _from_hls(h: float, l: float, s: float) -> str:
    return ''.join(f'{max(0, min(255, round(c * 255))):02X}' for c in colorsys.hls_to_rgb(h, l, s))


def _copy(ds: DesignSystem) -> DesignSystem:
    return DesignSystem.from_dict(copy.deepcopy(ds.to_dict()))


def is_dark(bg: str) -> bool:
    return _th()._lum(bg) < 0.2


def _best_on(back: str, cands) -> str:
    th = _th()
    return max(cands, key=lambda c: th.contrast(c, back))


def repair(ds: DesignSystem, *, quiet: bool = False, given=()) -> DesignSystem:
    """Every text/background pair at 4.5:1 (the second accent, drawn as shapes and large type, at 3:1), chart colours at
    3:1 on the background, text on accent fills and on photo overlays readable. A colour in `given` (set by the user's
    design.md or words) that had to move gets an honest note, unless `quiet`."""
    th = _th()
    ds = _copy(ds)
    c = ds.colors
    for role in COLOR_ROLES:   # a missing or malformed role is derived, never left out
        c[role] = _hex(c.get(role)) or _default_role(role, c)
    for fg, backs, ratio in TEXT_RULES:
        new = th.fit(c[fg], [c[b] for b in backs], ratio)
        if new != c[fg]:
            if fg in given and not quiet:
                dl = _hls(new)[1] - _hls(c[fg])[1]
                how = 'changed' if abs(dl) > 0.4 else ('lightened slightly' if dl > 0 else 'darkened slightly')
                ds.notes.append(f'The {ROLE_LABEL[fg]} was {how} to stay readable on its background.')
            c[fg] = new
    if th.contrast(c['on_accent'], c['accent']) < 4.5:
        best = _best_on(c['accent'], ('FFFFFF', '000000', c['text'], c['bg']))
        c['on_accent'] = best if th.contrast(best, c['accent']) >= 4.5 else th.fit(c['on_accent'], [c['accent']])
    pal = [p for p in (_hex(x) for x in ds.chart_palette or []) if p]
    if len(pal) < 6:
        from .presets import OKABE_DARK, OKABE_LIGHT
        pal += [p for p in (OKABE_DARK if is_dark(c['bg']) else OKABE_LIGHT) if p not in pal][:6 - len(pal)]
    fixed = [th.nudge(p, c['bg'], 3.0) for p in pal[:6]]
    if fixed != pal[:6] and 'palette' in given and not quiet and not ds.hatch:
        ds.notes.append('The chart colours were adjusted slightly to stay visible on the background.')
    ds.chart_palette = fixed
    # text over photos: the overlay is a tint of the background; raise its opacity until `text` reads against the
    # worst pixel a photo could have under it (white under a dark tint, black under a light one)
    worst = 'FFFFFF' if th._lum(c['overlay']) < 0.18 else '000000'
    alpha = max(0.3, min(0.95, float(ds.overlay_alpha or 0.55)))
    while alpha < 0.95 and th.contrast(c['text'], th.mix(worst, c['overlay'], alpha)) < 4.5:
        alpha = round(alpha + 0.05, 2)
    if th.contrast(c['text'], th.mix(worst, c['overlay'], alpha)) < 4.5:
        c['overlay'] = _best_on(c['text'], ('000000', 'FFFFFF'))
        worst = 'FFFFFF' if c['overlay'] == '000000' else '000000'
        alpha = 0.55
        while alpha < 0.95 and th.contrast(c['text'], th.mix(worst, c['overlay'], alpha)) < 4.5:
            alpha = round(alpha + 0.05, 2)
    ds.overlay_alpha = alpha
    ds.dark = is_dark(c['bg'])
    if ds.families.body and ds.justify and ds.id == 'high-legibility':
        ds.justify = False
    return ds


def _default_role(role: str, c: dict) -> str:
    th = _th()
    bg = _hex(c.get('bg')) or 'FFFFFF'
    dark = th._lum(bg) < 0.2
    text = _hex(c.get('text')) or ('F3F4F6' if dark else '1F2328')
    accent = _hex(c.get('accent')) or ('60A5FA' if dark else '1D4ED8')
    return {'bg': bg, 'text': text, 'surface': bg, 'heading': text, 'muted': th.mix(text, bg, 0.35), 'accent': accent,
            'accent2': _hex(c.get('accent')) or accent, 'border': th.mix(bg, text, 0.15),
            'header_bg': _hex(c.get('heading')) or accent, 'header_text': 'FFFFFF',
            'stripe': th._tint(bg, text, 0.04), 'code_bg': th._tint(bg, text, 0.06), 'overlay': bg,
            'on_accent': _best_on(accent, ('FFFFFF', '000000'))}[role]


# ---------- variants ----------


def with_dark(ds: DesignSystem, dark: bool) -> DesignSystem:
    """The same system on a dark (or light) background: colours re-derived by role, contrast repaired."""
    th = _th()
    dark = bool(dark)
    if ds.dark == dark and is_dark(ds.colors.get('bg') or 'FFFFFF') == dark:
        return _copy(ds)
    ds = _copy(ds)
    c = ds.colors
    acc = c.get('accent') or '2563EB'
    h, _, s = _hls(acc)
    grey = s < 0.08
    if dark:
        bg = _from_hls(h, 0.07, 0 if grey else min(s, 0.45))
        text = 'F3F4F6'
        n = dict(bg=bg, surface=_from_hls(h, 0.11, 0 if grey else min(s, 0.4)), text=text,
                 heading=_from_hls(h, 0.88, 0 if grey else min(s, 0.6)), muted=th.mix(text, bg, 0.32),
                 accent=th.fit(acc, [bg]), accent2=th.nudge(c.get('accent2') or acc, bg, 3.0),
                 border=_from_hls(h, 0.22, 0 if grey else min(s, 0.3)),
                 header_bg=_from_hls(h, 0.2, 0 if grey else min(s, 0.5)), header_text='FFFFFF',
                 stripe=th.mix(bg, text, 0.05), code_bg=_from_hls(h, 0.12, 0 if grey else min(s, 0.4)), overlay=bg)
    else:
        bg = 'FFFFFF'
        text = '1F2328'
        n = dict(bg=bg, surface=_from_hls(h, 0.97, 0 if grey else min(s, 0.3)), text=text,
                 heading=_from_hls(h, 0.25, 0 if grey else s), muted='57606A', accent=th.fit(acc, [bg]),
                 accent2=th.nudge(c.get('accent2') or acc, bg, 3.0), border='D0D7DE',
                 header_bg=_from_hls(h, 0.25, 0 if grey else min(s, 0.6)), header_text='FFFFFF',
                 stripe=th.mix(bg, text, 0.04), code_bg='F4F4F5', overlay=bg)
    n['on_accent'] = _best_on(n['accent'], ('FFFFFF', '000000'))
    c.update(n)
    ds.chart_palette = [th.nudge(p, bg, 3.0) for p in ds.chart_palette] if ds.chart_palette else []
    ds.gradients = [(bg, th.mix(bg, n['accent'], 0.3)), (n['accent'], n['accent2'])]
    ds.dark = dark
    base = re.sub(r'\s*\((?:dark|light)\)$', '', ds.name)
    ds.name = f'{base} ({"dark" if dark else "light"})'
    ds.source = [*ds.source, 'dark' if dark else 'light']
    return repair(ds, quiet=True)


def _grey(h: str) -> float:
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def palette_survives_greyscale(palette: list[str], min_step: float = 0.08) -> bool:
    """True when every pair of chart colours is still told apart once printed in grey."""
    g = sorted(_grey(p) for p in palette)
    return all(b - a >= min_step for a, b in zip(g, g[1:]))


def print_version(ds: DesignSystem) -> DesignSystem:
    """Grayscale-safe, white-page variant for printing (hatch charts when the palette doesn't survive greyscale)."""
    th = _th()
    ds = with_dark(ds, False) if ds.dark else _copy(ds)
    c = ds.colors
    c.update(bg='FFFFFF', surface='FFFFFF', stripe='F2F2F2', code_bg='F2F2F2', overlay='FFFFFF')
    c['text'] = th.fit(c['text'], ['FFFFFF', 'F2F2F2'], 7.0)
    c['heading'] = th.fit(c['heading'], ['FFFFFF'], 7.0)
    c['header_bg'] = th.fit(c['header_bg'], ['FFFFFF'], 7.0)
    ds.chart_palette = [th.nudge(p, 'FFFFFF', 3.0) for p in ds.chart_palette]
    if not palette_survives_greyscale(ds.chart_palette):
        ds.hatch = True
    ds.shape.shadow = False
    if ds.image in ('full-bleed', 'duotone'):
        ds.image = 'framed'
    ds.gradients = [('FFFFFF', 'F2F2F2')]
    ds.source = [*ds.source, 'print']
    return repair(ds, quiet=True)


# ---------- design.md ----------


def _clean_design(design) -> dict | None:
    if not isinstance(design, dict):
        return None
    try:
        from ..create.design import clean_design
        return clean_design(design)
    except Exception:
        return None


def from_design(design: dict) -> dict:
    """The overrides a cleaned design.md dict gives (colours, families, scale ratio, spacing unit, radius, image rules,
    do/don't), as a partial DesignSystem dict. Unknown keys ignored."""
    out: dict = {}
    if not isinstance(design, dict):
        return out
    raw = design.get('colors') if isinstance(design.get('colors'), dict) else {}
    colors = {r: h for r in COLOR_ROLES if (h := _hex(raw.get(r)))}
    if colors:
        out['colors'] = colors
    pal = [h for h in (_hex(x) for x in (design.get('palette') or []) if isinstance(design.get('palette'), list)) if h]
    if len(pal) >= 3 and not design.get('hatch'):
        out['chart_palette'] = pal[:6]
    if design.get('hatch') and len(pal) < 3:
        out['hatch'] = True
    fams = {}
    for key_, roles in (('heading_font', ('display', 'heading')), ('body_font', ('body', 'caption')),
                        ('mono_font', ('mono',))):
        f = design.get(key_)
        fam = str(f.get('family') or '').strip() if isinstance(f, dict) else ''
        if fam and fam.lower() not in GENERIC_FONTS:
            for r in roles:
                fams[r] = fam[:64]
    if 'body' in fams and 'display' not in fams:
        fams['display'] = fams['heading'] = fams['body']
    if fams:
        out['families'] = fams
    radius = design.get('radius')
    sysd = design.get('system') if isinstance(design.get('system'), dict) else {}
    if sysd.get('radius') is not None:
        radius = sysd.get('radius')
    try:
        if radius is not None:
            out['shape'] = {'radius': float(max(0, min(48, float(radius))))}
    except (TypeError, ValueError):
        pass
    try:
        if sysd.get('scale_ratio') is not None:
            out['scale'] = {'ratio': round(max(1.1, min(1.7, float(sysd['scale_ratio']))), 3)}
    except (TypeError, ValueError):
        pass
    try:
        if sysd.get('spacing_unit') is not None:
            out['spacing'] = {'unit': float(max(2, min(16, float(sysd['spacing_unit']))))}
    except (TypeError, ValueError):
        pass
    if sysd.get('image') in IMAGE_TREATMENTS:
        out['image'] = sysd['image']
    for k in ('do', 'dont'):
        lines = [re.sub(r'[\x00-\x1f]', ' ', str(x)).strip()[:200] for x in sysd.get(k) or [] if isinstance(x, str)]
        if lines := [x for x in lines if x][:12]:
            out[k] = lines
    if design.get('name'):
        out['name'] = str(design['name'])[:120]
    return out


GENERIC_FONTS = {'serif', 'sans-serif', 'sans', 'monospace', 'mono', 'ui-sans-serif', 'ui-serif', 'ui-monospace',
                 'system-ui', 'ui-rounded', 'cursive', 'fantasy', '-apple-system', 'blinkmacsystemfont', 'inherit'}


def _open_family(name: str, notes: list[str]) -> str:
    """A requested family as the file will use it: itself when it is open or installed (or the user's own font file),
    else its closest open alternative with an honest note."""
    from . import fonts
    fam = fonts.display(name)
    alt = fonts.alternative(fam)
    if alt is not None and fonts.resolve_local(fam) is None:
        if alt[1] not in notes:
            notes.append(alt[1])
        return alt[0]
    return fam


def _apply_fonts(ds: DesignSystem, wanted: dict) -> None:
    for role, fam in wanted.items():
        if role in FONT_ROLES and fam:
            setattr(ds.families, role, _open_family(str(fam), ds.notes))


def _apply_design(ds: DesignSystem, over: dict) -> DesignSystem:
    colors = over.get('colors') or {}
    if colors.get('bg') and is_dark(colors['bg']) != ds.dark:
        ds = with_dark(ds, is_dark(colors['bg']))
    th = _th()
    if colors.get('bg'):
        bg, text = colors['bg'], colors.get('text') or ds.colors['text']
        derived = {'surface': bg, 'stripe': th._tint(bg, text, 0.04), 'code_bg': th._tint(bg, text, 0.06),
                   'border': th.mix(bg, text, 0.15), 'overlay': bg}
        for role in BG_DERIVED:
            if role not in colors:
                ds.colors[role] = derived[role]
    ds.colors.update(colors)
    if 'chart_palette' in over:
        ds.chart_palette = list(over['chart_palette'])
    if over.get('hatch'):
        ds.hatch = True
    _apply_fonts(ds, over.get('families') or {})
    for part in ('shape', 'scale', 'spacing'):
        for k, v in (over.get(part) or {}).items():
            setattr(getattr(ds, part), k, v)
    if over.get('image'):
        ds.image = over['image']
    ds.do = list(over.get('do') or ds.do)
    ds.dont = list(over.get('dont') or ds.dont)
    return ds


# ---------- building ----------


def build_system(*, preset: str | None = None, design: dict | None = None, prompt: str = '', fmt: str = 'pptx',
                 font: str | None = None, dark: bool | None = None) -> DesignSystem:
    """The file's DesignSystem: design (spec['design'], create/design.clean_design output, v2 keys included) wins over
    prompt words (presets.read_prompt(prompt)), which win over `preset`, then presets.DEFAULT_PRESET. `font` is the
    brief's body font request; `dark` forces the dark or light variant. Every text/background pair is repaired to 4.5:1
    (3:1 for display) and said in `notes`. Never raises: unknown input falls back to the default preset."""
    from . import presets as P
    try:
        return _build(P, preset, design, prompt, fmt, font, dark)
    except Exception:
        ds = P.get(P.DEFAULT_PRESET)
        ds.source = ['default']
        ds.notes.append('The style settings could not be read, so the standard style was used.')
        return ds


def _build(P, preset, design, prompt, fmt, font, dark) -> DesignSystem:
    style = P.read_prompt(prompt) if prompt else P.PromptStyle()
    base_src: list[str] = []
    prompt_src: list[str] = []
    if style.preset:
        pid = style.preset
        prompt_src.append(f'prompt:{pid}')
    elif preset in P.PRESETS:
        pid = preset
        base_src.append(f'preset:{pid}')
    elif preset in P.LEGACY_THEME:
        pid = P.LEGACY_THEME[preset]
        base_src.append(f'theme:{preset}')
    else:
        pid = P.DEFAULT_PRESET
        base_src.append('default')
    ds = P.get(pid)
    notes: list[str] = []
    if style.dark is not None:
        prompt_src.append('prompt:dark' if style.dark else 'prompt:light')
        if style.dark != ds.dark:
            ds = with_dark(ds, style.dark)
    if style.print_version:
        prompt_src.append('prompt:print')
        ds = print_version(ds)
    wanted: dict[str, str] = {}
    if font:
        wanted['body'] = str(font)
    for role, fam in (style.fonts or {}).items():
        wanted[role] = fam
    if wanted:
        prompt_src.append('prompt:fonts')
    ds.notes = []
    _apply_fonts(ds, wanted)
    notes += ds.notes
    given: set[str] = set()
    design_src: list[str] = []
    over = from_design(_clean_design(design) or {}) if design else {}
    if over:
        design_src.append('design.md')
        given = set(over.get('colors') or {})
        if 'chart_palette' in over:
            given.add('palette')
        ds.notes = []
        ds = _apply_design(ds, over)
        notes += ds.notes
    if dark is not None and bool(dark) != ds.dark:
        ds = with_dark(ds, bool(dark))
        base_src.insert(0, 'dark' if dark else 'light')
    ds.notes = []
    ds = repair(ds, given=given)
    ds.notes = list(dict.fromkeys([*notes, *ds.notes]))
    ds.source = [*design_src, *prompt_src, *base_src]
    if pid == 'high-legibility':
        ds.justify = False
    return ds


# ---------- legacy renderers ----------


def legacy_theme(ds: DesignSystem, fmt: str) -> dict:
    """A create/themes.py-compatible theme dict (bg, text, muted, heading, accent, header_bg, header_text, stripe,
    code_bg, border, palette, pdf_font, font, heading_font, mono, hatch, grey_images), so the legacy renderers (docx,
    md, xlsx, and the fallback path) draw with the same tokens."""
    from . import fonts
    th = _th()
    src = with_dark(ds, False) if ds.dark and fmt in ('docx', 'xlsx') else ds   # Word and Excel are read on white pages
    c = src.colors
    cat = fonts.category_of(src.families.body)
    t = {k: c[k] for k in ('bg', 'surface', 'text', 'muted', 'heading', 'accent', 'header_bg', 'header_text',
                           'stripe', 'code_bg', 'border')}
    backs: dict[str, list[str]] = {}
    for fg, back in th.TEXT_PAIRS:   # the legacy rules check these pairs at 4.5:1
        backs.setdefault(fg, []).append(back)
    for fg, names in backs.items():
        t[fg] = th.fit(t[fg], [t[b] for b in names])
    t['palette'] = list(src.chart_palette)
    t['pdf_font'] = th.PDF_BUILTIN.get(cat if cat in th.PDF_BUILTIN else 'sans', th.PDF_BUILTIN['sans'])
    t['font'] = fonts.office_fallback(src.families.body)
    t['heading_font'] = fonts.office_fallback(src.families.heading)
    t['mono'] = fonts.office_fallback(src.families.mono) if fonts.category_of(src.families.mono) == 'mono' else \
        'Consolas'
    t['font_stack'] = [src.families.body, t['font']]
    t['font_category'] = cat if cat in ('sans', 'serif', 'mono') else 'sans'
    t['hatch'] = bool(src.hatch)
    t['patterns'] = bool(src.hatch)
    t['grey_images'] = bool(src.grey_images)
    t['radius'] = int(round(src.shape.radius))
    t['preset'] = ds.id
    t['nudged'] = []
    t['design_notes'] = list(ds.notes)
    return t
