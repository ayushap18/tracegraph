"""Design tokens (docs/PLAN-designer.md 3.2 and 9.3): one DesignSystem per file, built in priority order from an attached
design.md (create/design.py), explicit prompt words (presets.read_prompt), the chosen preset, then defaults.

Owner: builder F. The dataclasses are contract (fields are fixed); the functions are stubs until F builds them.
"""
from __future__ import annotations

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


def build_system(*, preset: str | None = None, design: dict | None = None, prompt: str = '', fmt: str = 'pptx',
                 font: str | None = None, dark: bool | None = None) -> DesignSystem:
    """The file's DesignSystem: design (spec['design'], create/design.clean_design output, v2 keys included) wins over
    prompt words (presets.read_prompt(prompt)), which win over `preset`, then presets.DEFAULT_PRESET. `font` is the
    brief's body font request; `dark` forces the dark or light variant. Every text/background pair is repaired to 4.5:1
    (3:1 for display) and said in `notes`. Never raises: unknown input falls back to the default preset."""
    raise NotImplementedError('studio.tokens.build_system: builder F')


def with_dark(ds: DesignSystem, dark: bool) -> DesignSystem:
    """The same system on a dark (or light) background: colours re-derived by role, contrast repaired."""
    raise NotImplementedError('studio.tokens.with_dark: builder F')


def print_version(ds: DesignSystem) -> DesignSystem:
    """Grayscale-safe, white-page variant for printing (hatch charts when the palette doesn't survive greyscale)."""
    raise NotImplementedError('studio.tokens.print_version: builder F')


def from_design(design: dict) -> dict:
    """The overrides a cleaned design.md dict gives (colours, families, scale ratio, spacing unit, radius, image rules,
    do/don't), as a partial DesignSystem dict. Unknown keys ignored."""
    raise NotImplementedError('studio.tokens.from_design: builder F')


def legacy_theme(ds: DesignSystem, fmt: str) -> dict:
    """A create/themes.py-compatible theme dict (bg, text, muted, heading, accent, header_bg, header_text, stripe,
    code_bg, border, palette, pdf_font, font, heading_font, mono, hatch, grey_images), so the legacy renderers (docx,
    md, xlsx, and the fallback path) draw with the same tokens."""
    raise NotImplementedError('studio.tokens.legacy_theme: builder F')
