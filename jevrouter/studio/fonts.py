"""The font manager (docs/PLAN-designer.md 3.3 and 9.4): cached, installed and open-licensed downloaded families,
closest-open-alternative for proprietary requests, real metrics from fontTools, embedding info per format.

Owner: builder F. create/fonts.resolve() stays the legacy entry point and becomes an adapter over this module (F).
Downloads only through the existing egress guard (create/assets.guarded_session), only for LICENCES_OK.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / 'data' / 'cache' / 'fonts'   # <family-key>/<style>.ttf + LICENSE + meta.json
CACHE_BYTES = 200 * 1024 * 1024
MAX_FONT_BYTES = 8 * 1024 * 1024                # one downloaded file
STYLES = ('regular', 'bold', 'italic', 'bolditalic')
LICENCES_OK = ('OFL-1.1', 'Apache-2.0', 'UFL-1.0')
SOURCES = ('cache', 'system', 'fontsource', 'google-fonts', 'user', 'builtin')
CATEGORIES = ('sans', 'serif', 'mono', 'display', 'handwriting')
# proprietary or unavailable family (lower case) -> closest open family; F extends it (category, x-height, width,
# contrast). The note: "<Requested> isn't openly licensed, so this uses <Open>, the closest open match."
SIMILAR: dict[str, str] = {
    'anthropic sans': 'Inter', 'anthropic serif': 'Source Serif 4', 'helvetica neue': 'Inter', 'helvetica': 'Inter',
    'arial': 'Arimo', 'sf pro': 'Inter', 'sf pro display': 'Inter', 'segoe ui': 'Open Sans', 'calibri': 'Carlito',
    'cambria': 'Caladea', 'times new roman': 'Tinos', 'georgia': 'Gelasio', 'futura': 'Jost', 'gill sans': 'Lato',
    'avenir': 'Nunito Sans', 'proxima nova': 'Montserrat', 'garamond': 'EB Garamond', 'courier new': 'Cousine',
    'century gothic': 'Questrial', 'franklin gothic': 'Libre Franklin', 'myriad pro': 'PT Sans',
}


@dataclass
class FontFace:
    family: str
    style: str               # STYLES
    path: str                # absolute .ttf/.otf path
    licence: str             # LICENCES_OK value, 'system', 'user' or 'builtin'
    source: str              # SOURCES


@dataclass
class FontMetrics:
    units_per_em: int
    ascender: int
    descender: int           # negative
    line_gap: int
    x_height: int
    cap_height: int
    widths: dict[int, int] = field(default_factory=dict)   # codepoint -> advance (font units)
    default_width: int = 0   # advance for codepoints the font lacks (.notdef)

    def advance(self, text: str, size_pt: float) -> float:
        """Width of `text` at `size_pt` in points, no kerning."""
        w = sum(self.widths.get(ord(c), self.default_width) for c in text)
        return w * size_pt / self.units_per_em


@dataclass
class FontResolution:
    requested: str | None    # what the user or design asked for
    family: str              # what the file uses
    faces: dict[str, FontFace]   # style -> face; may be empty for 'builtin' (PDF standard fonts)
    office_fallback: str     # the installed-everywhere family PowerPoint/Word falls back to (Calibri, Georgia, ...)
    licence: str
    source: str
    embeddable: bool         # PDF can subset-embed it
    note: str | None = None  # honest sentence when family != requested, or "install X (free) for the exact look"
    install_url: str | None = None   # where the user can get the free font (pptx/docx card)


@dataclass
class FontInfo:
    family: str
    category: str            # CATEGORIES
    licence: str             # LICENCES_OK value
    source: str              # SOURCES
    styles: list[str]
    installed: bool          # in the cache or the system already
    preview: str             # '/api/fonts/preview?family=<urlencoded>'

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_local(family: str) -> FontResolution | None:
    """No network: the family from the cache, then the system's installed fonts; None when neither has it."""
    raise NotImplementedError('studio.fonts.resolve_local: builder F')


async def ensure(family: str, http, *, styles: tuple[str, ...] = STYLES) -> FontResolution:
    """The family ready to use: cache, system, then an open-licensed download (Fontsource via jsDelivr, then the
    google/fonts repository), the licence read from source metadata before download and stored beside the files. A
    proprietary or unknown family resolves to alternative(); offline or http None resolves locally and never raises."""
    raise NotImplementedError('studio.fonts.ensure: builder F')


def alternative(family: str) -> tuple[str, str] | None:
    """(open family, note) for a family that can't be downloaded; None when `family` is itself open."""
    raise NotImplementedError('studio.fonts.alternative: builder F')


def metrics(path: str) -> FontMetrics:
    """fontTools metrics of one font file (cached per path)."""
    raise NotImplementedError('studio.fonts.metrics: builder F')


def measure(text: str, family: str, size_pt: float, *, bold: bool = False, italic: bool = False,
            fmt: str = 'pptx') -> float:
    """Width in points. For pptx/docx, the wider of the family and its office_fallback, so text fits either way."""
    raise NotImplementedError('studio.fonts.measure: builder F')


def wrap(text: str, family: str, size_pt: float, width_pt: float, *, bold: bool = False, italic: bool = False,
         fmt: str = 'pptx') -> list[str]:
    """`text` greedily wrapped to `width_pt` with measure(); a word wider than the line is broken by characters."""
    raise NotImplementedError('studio.fonts.wrap: builder F')


def line_metrics(family: str, size_pt: float, line_height: float = 1.2) -> tuple[float, float, float]:
    """(ascent, descent, line advance) in points for a family at a size."""
    raise NotImplementedError('studio.fonts.line_metrics: builder F')


async def search(q: str, http, limit: int = 20) -> tuple[list[FontInfo], bool]:
    """(open-licensed families matching q, offline). Empty q: a curated popular list. Never returns non-open fonts."""
    raise NotImplementedError('studio.fonts.search: builder F')


def preview_png(family: str, text: str = 'The quick brown fox', px: int = 32) -> bytes | None:
    """A PNG sample of a cached or installed open family (GET /api/fonts/preview); None when unavailable."""
    raise NotImplementedError('studio.fonts.preview_png: builder F')


def prune(cap_bytes: int = CACHE_BYTES) -> int:
    """LRU-evict cached families above the cap; returns bytes freed."""
    raise NotImplementedError('studio.fonts.prune: builder F')
