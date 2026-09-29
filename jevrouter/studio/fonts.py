"""The font manager (docs/PLAN-designer.md 3.3 and 9.4): cached, installed and open-licensed downloaded families,
closest-open-alternative for proprietary requests, real metrics from fontTools, embedding info per format.

Owner: builder F. create/fonts.resolve() stays the legacy entry point and becomes an adapter over this module (F).
Downloads only through the existing egress guard (create/assets.guarded_session), only for LICENCES_OK.

Sources, in order: the shared cache (data/cache/fonts/<family-key>/<style>.ttf + LICENSE + meta.json), the system's
installed TrueType fonts (create/fonts.system_index), then an open-licensed download from Fontsource (metadata from
api.fontsource.org, files from the jsDelivr CDN) or the google/fonts repository. The licence is read from the source's
metadata before any font file is fetched; anything outside LICENCES_OK is never downloaded. Offline, or when a download
fails, a family resolves to an installed or built-in font of the same category: ensure() never raises.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

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
    # F's extensions: grouped by what the open family shares with the requested one (category, x-height, width)
    'anthropic mono': 'JetBrains Mono', 'styrene': 'Inter', 'styrene a': 'Inter', 'styrene b': 'Inter',
    'tiempos': 'Source Serif 4', 'tiempos text': 'Source Serif 4', 'tiempos headline': 'Source Serif 4',
    'copernicus': 'Source Serif 4', 'sf pro text': 'Inter', 'sf pro rounded': 'Nunito', 'sf compact': 'Inter',
    'san francisco': 'Inter', 'sf mono': 'JetBrains Mono', 'new york': 'Source Serif 4', 'segoe ui variable': 'Open Sans',
    'helvetica neue lt': 'Inter', 'neue haas grotesk': 'Inter', 'neue haas unica': 'Inter', 'akzidenz grotesk': 'Inter',
    'graphik': 'Inter', 'circular': 'DM Sans', 'circular std': 'DM Sans', 'gotham': 'Montserrat', 'avenir next': 'Nunito Sans',
    'univers': 'Roboto', 'frutiger': 'Open Sans', 'din': 'Barlow', 'din next': 'Barlow', 'verdana': 'DejaVu Sans',
    'tahoma': 'PT Sans', 'trebuchet ms': 'Fira Sans', 'lucida grande': 'Open Sans', 'optima': 'Tenor Sans',
    'google sans': 'Outfit', 'product sans': 'Outfit', 'baskerville': 'Libre Baskerville', 'bodoni': 'Bodoni Moda',
    'didot': 'Playfair Display', 'minion pro': 'Crimson Pro', 'palatino': 'Crimson Pro', 'palatino linotype': 'Crimson Pro',
    'book antiqua': 'Crimson Pro', 'adobe garamond': 'EB Garamond', 'rockwell': 'Roboto Slab', 'consolas': 'Inconsolata',
    'menlo': 'JetBrains Mono', 'monaco': 'JetBrains Mono', 'comic sans': 'Comic Neue', 'comic sans ms': 'Comic Neue',
    'impact': 'Anton', 'arial black': 'Archivo', 'arial narrow': 'Archivo', 'calibri light': 'Carlito',
    'aptos': 'Inter', 'aptos display': 'Inter', 'candara': 'PT Sans', 'constantia': 'Gelasio', 'corbel': 'Open Sans',
    'sabon': 'EB Garamond', 'caslon': 'Libre Baskerville', 'myriad': 'PT Sans',
}
# the open family has the same letter widths as the requested one, so text fits exactly as it would
METRIC_COMPATIBLE = {'calibri': 'Carlito', 'cambria': 'Caladea', 'arial': 'Arimo', 'times new roman': 'Tinos',
                     'courier new': 'Cousine'}

# Curated open families: family -> (category, licence). search('') lists them; they also give categories offline.
# Checked against Fontsource's metadata (OFL-1.1 unless noted).
CATALOG: dict[str, tuple[str, str]] = {
    'Inter': ('sans', 'OFL-1.1'), 'Roboto': ('sans', 'OFL-1.1'), 'Open Sans': ('sans', 'OFL-1.1'),
    'Lato': ('sans', 'OFL-1.1'), 'Montserrat': ('sans', 'OFL-1.1'), 'Poppins': ('sans', 'OFL-1.1'),
    'Nunito': ('sans', 'OFL-1.1'), 'Nunito Sans': ('sans', 'OFL-1.1'), 'Source Sans 3': ('sans', 'OFL-1.1'),
    'Source Serif 4': ('serif', 'OFL-1.1'), 'Source Code Pro': ('mono', 'OFL-1.1'), 'Merriweather': ('serif', 'OFL-1.1'),
    'Playfair Display': ('serif', 'OFL-1.1'), 'Lora': ('serif', 'OFL-1.1'), 'EB Garamond': ('serif', 'OFL-1.1'),
    'Libre Baskerville': ('serif', 'OFL-1.1'), 'Crimson Pro': ('serif', 'OFL-1.1'), 'Space Grotesk': ('sans', 'OFL-1.1'),
    'DM Sans': ('sans', 'OFL-1.1'), 'Work Sans': ('sans', 'OFL-1.1'), 'Raleway': ('sans', 'OFL-1.1'),
    'Oswald': ('display', 'OFL-1.1'), 'Bebas Neue': ('display', 'OFL-1.1'), 'Fredoka': ('display', 'OFL-1.1'),
    'Quicksand': ('sans', 'OFL-1.1'), 'Atkinson Hyperlegible': ('sans', 'OFL-1.1'), 'Lexend': ('sans', 'OFL-1.1'),
    'JetBrains Mono': ('mono', 'OFL-1.1'), 'Fira Sans': ('sans', 'OFL-1.1'), 'Fira Code': ('mono', 'OFL-1.1'),
    'IBM Plex Sans': ('sans', 'OFL-1.1'), 'IBM Plex Serif': ('serif', 'OFL-1.1'), 'IBM Plex Mono': ('mono', 'OFL-1.1'),
    'Arimo': ('sans', 'OFL-1.1'), 'Tinos': ('serif', 'OFL-1.1'), 'Cousine': ('mono', 'OFL-1.1'),
    'Carlito': ('sans', 'OFL-1.1'), 'Caladea': ('serif', 'OFL-1.1'), 'Gelasio': ('serif', 'OFL-1.1'),
    'Jost': ('sans', 'OFL-1.1'), 'Questrial': ('sans', 'OFL-1.1'), 'Libre Franklin': ('sans', 'OFL-1.1'),
    'PT Sans': ('sans', 'OFL-1.1'), 'PT Serif': ('serif', 'OFL-1.1'), 'Noto Sans': ('sans', 'OFL-1.1'),
    'Noto Serif': ('serif', 'OFL-1.1'), 'Caveat': ('handwriting', 'OFL-1.1'), 'Patrick Hand': ('handwriting', 'OFL-1.1'),
    'Comic Neue': ('handwriting', 'OFL-1.1'), 'Barlow': ('sans', 'OFL-1.1'), 'Inconsolata': ('mono', 'OFL-1.1'),
    'Anton': ('display', 'OFL-1.1'), 'Roboto Slab': ('serif', 'Apache-2.0'), 'Bodoni Moda': ('serif', 'OFL-1.1'),
    'Outfit': ('sans', 'OFL-1.1'), 'Tenor Sans': ('sans', 'OFL-1.1'), 'Ubuntu': ('sans', 'UFL-1.0'),
    'Manrope': ('sans', 'OFL-1.1'), 'Rubik': ('sans', 'OFL-1.1'), 'Figtree': ('sans', 'OFL-1.1'),
    'Archivo': ('sans', 'OFL-1.1'), 'Literata': ('serif', 'OFL-1.1'), 'Andika': ('sans', 'OFL-1.1'),
    'OpenDyslexic': ('sans', 'OFL-1.1'), 'DejaVu Sans': ('sans', 'OFL-1.1'), 'Space Mono': ('mono', 'OFL-1.1'),
}
# high-legibility families (the high-legibility preset; dyslexia-friendly requests)
LEGIBLE = ('Atkinson Hyperlegible', 'Lexend', 'Andika', 'OpenDyslexic')
# what a category falls back to: cached open families first, then installed ones, then the PDF standard font
CATEGORY_OPEN = {'sans': ('Inter', 'Open Sans', 'Arimo', 'Carlito', 'DejaVu Sans'),
                 'serif': ('Source Serif 4', 'Tinos', 'Gelasio', 'Caladea'),
                 'mono': ('JetBrains Mono', 'Cousine', 'Inconsolata'),
                 'display': ('Inter', 'Open Sans'), 'handwriting': ('Comic Neue', 'Inter')}
CATEGORY_SYSTEM = {'sans': ('Arial', 'Verdana', 'DejaVu Sans', 'Liberation Sans', 'Tahoma'),
                   'serif': ('Georgia', 'Times New Roman', 'DejaVu Serif', 'Liberation Serif'),
                   'mono': ('Courier New', 'DejaVu Sans Mono', 'Liberation Mono', 'Andale Mono'),
                   'display': ('Arial', 'Verdana'), 'handwriting': ('Comic Sans MS', 'Arial')}
CATEGORY_BUILTIN = {'sans': 'Helvetica', 'serif': 'Times-Roman', 'mono': 'Courier', 'display': 'Helvetica',
                    'handwriting': 'Helvetica'}
# the family Office shows when a named font isn't installed, and the open clone used to measure it
OFFICE_FALLBACK = {'sans': 'Calibri', 'serif': 'Georgia', 'mono': 'Consolas', 'display': 'Calibri',
                   'handwriting': 'Calibri'}
OFFICE_FONTS = ('Calibri', 'Arial', 'Segoe UI', 'Georgia', 'Cambria', 'Times New Roman', 'Consolas', 'Courier New',
                'Verdana', 'Tahoma', 'Trebuchet MS', 'Aptos')
OFFICE_CLONE = {'calibri': 'Carlito', 'cambria': 'Caladea', 'arial': 'Arimo', 'times new roman': 'Tinos',
                'courier new': 'Cousine', 'georgia': 'Gelasio', 'consolas': 'Inconsolata', 'segoe ui': 'Open Sans'}

FONTSOURCE_API = 'https://api.fontsource.org/v1/fonts'
JSDELIVR = 'https://cdn.jsdelivr.net'
GOOGLE_FONTS = 'https://raw.githubusercontent.com/google/fonts/main'
META_BYTES = 4 * 1024 * 1024      # the Fontsource index is about 0.5 MB
LICENCE_BYTES = 256 * 1024
INDEX_TTL = 7 * 24 * 3600
DOWNLOAD_TIMEOUT = 15.0
STYLE_VARIANT = {'regular': (400, 'normal'), 'bold': (700, 'normal'), 'italic': (400, 'italic'),
                 'bolditalic': (700, 'italic')}
LICENCE_URL = {'OFL-1.1': 'https://openfontlicense.org/open-font-license-official-text/',
               'Apache-2.0': 'https://www.apache.org/licenses/LICENSE-2.0',
               'UFL-1.0': 'https://ubuntu.com/legal/font-licence'}


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


class FetchError(Exception):
    """A download that failed or was refused by the egress guard."""


class LicenceRefused(Exception):
    """The source says the family is not under an open licence; nothing was downloaded."""

    def __init__(self, family: str, licence: str):
        super().__init__(f'{family}: {licence}')
        self.family, self.licence = family, licence


# ---------- names ----------


def key(name) -> str:
    """'Source Serif 4' -> 'sourceserif4' (the cache folder name and the comparison key)."""
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def slug(name) -> str:
    """'Source Serif 4' -> 'source-serif-4' (the Fontsource id)."""
    return re.sub(r'[^a-z0-9]+', '-', str(name or '').lower()).strip('-')


def clean(name) -> str:
    """A family name made safe and tidy: control characters dropped, spaces collapsed, at most 64 characters."""
    s = ' '.join(re.sub(r'[\x00-\x1f<>"\\/]', ' ', str(name or '')).split())[:64]
    return s


def display(name) -> str:
    """The family as people write it: a known name's own spelling, else Title Case for a lower-case request."""
    s = clean(name)
    k = key(s)
    for fam in (*CATALOG, *OFFICE_FONTS):
        if key(fam) == k:
            return fam
    if s and s == s.lower():
        from ..create.fonts import display as legacy_display
        return legacy_display(s)
    return s


_SIMILAR_KEY = {key(k): v for k, v in SIMILAR.items()}
_CATALOG_KEY = {key(k): k for k in CATALOG}


def category_of(family) -> str:
    """The family's category from the catalog, the cache, the similarity table or its name; 'sans' when unknown."""
    k = key(family)
    if k in _CATALOG_KEY:
        return CATALOG[_CATALOG_KEY[k]][0]
    meta = _read_meta(k)
    if meta and meta.get('category') in CATEGORIES:
        return meta['category']
    if k in _SIMILAR_KEY:
        return category_of(_SIMILAR_KEY[k])
    low = str(family or '').lower()
    if re.search(r'mono|code|courier|consol', low):
        return 'mono'
    if re.search(r'serif', low) and 'sans' not in low or re.search(r'times|georgia|garamond|baskerville|cambria|'
                                                                     r'bodoni|didot|palatino|caslon|tiempos', low):
        return 'serif'
    return 'sans'


def office_fallback(family) -> str:
    """The family PowerPoint and Word show when `family` isn't installed: itself when Office ships it, else Calibri,
    Georgia or Consolas by category."""
    k = key(family)
    for f in OFFICE_FONTS:
        if key(f) == k:
            return f
    return OFFICE_FALLBACK.get(category_of(family), 'Calibri')


def install_url(family) -> str | None:
    """Where to get an open family for free (Fontsource's page), or None for fonts that aren't known to be open."""
    k = key(family)
    meta = _read_meta(k)
    if k in _CATALOG_KEY or (meta and meta.get('licence') in LICENCES_OK):
        return f'https://fontsource.org/fonts/{slug((meta or {}).get("family") or family)}'
    return None


def install_note(res: FontResolution, fmt: str) -> str | None:
    """The file card sentence for PowerPoint and Word, which name a font but can't embed it."""
    if fmt not in ('pptx', 'docx', 'xlsx') or res.source == 'builtin' or key(res.family) == key(res.office_fallback):
        return None
    app = {'pptx': 'PowerPoint', 'docx': 'Word', 'xlsx': 'Excel'}[fmt]
    if res.install_url:
        return (f'Install {res.family} (free) for the exact look. Where it is not installed, {app} shows '
                f'{res.office_fallback}, and the text still fits.')
    return f'{app} shows {res.family} only where it is installed; elsewhere it uses {res.office_fallback}.'


# ---------- the cache ----------


def _family_dir(k: str) -> Path:
    return Path(CACHE_DIR) / k


def _read_meta(k: str) -> dict | None:
    if not k:
        return None
    try:
        meta = json.loads((_family_dir(k) / 'meta.json').read_text('utf-8'))
        return meta if isinstance(meta, dict) else None
    except (OSError, ValueError):
        return None


def _touch(k: str) -> None:
    try:
        os.utime(_family_dir(k) / 'meta.json')
    except OSError:
        pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


def _env_font():
    try:
        from ..create.fonts import _env_font as legacy_env
        return legacy_env()
    except Exception:
        return None


def _system_index() -> dict:
    try:
        from ..create import fonts as legacy
        return legacy.system_index()
    except Exception:
        return {}


def _find_local(family: str, touch: bool) -> FontResolution | None:
    name = clean(family)
    k = key(name)
    if not k:
        return None
    env = _env_font()
    if env:
        from ..create.fonts import matches
        if matches(name, env[3]):
            faces = {'regular': FontFace(env[3], 'regular', env[0], 'user', 'user')}
            if env[1]:
                faces['bold'] = FontFace(env[3], 'bold', env[1], 'user', 'user')
            if env[2]:
                faces['italic'] = FontFace(env[3], 'italic', env[2], 'user', 'user')
            return FontResolution(None, env[3], faces, office_fallback(env[3]), 'user', 'user',
                                  env[0].lower().endswith('.ttf'))
    meta = _read_meta(k)
    if meta and meta.get('licence') in LICENCES_OK:
        fam = str(meta.get('family') or display(name))
        faces = {}
        for style, fname in (meta.get('files') or {}).items():
            p = _family_dir(k) / str(fname)
            if style in STYLES and p.is_file() and '/' not in str(fname):
                faces[style] = FontFace(fam, style, str(p), meta['licence'], 'cache')
        if 'regular' in faces:
            if touch:
                _touch(k)
            return FontResolution(None, fam, faces, office_fallback(fam), meta['licence'], 'cache',
                                  faces['regular'].path.lower().endswith('.ttf'), install_url=install_url(fam))
    found = _system_index().get(k)
    if found and found.get('regular'):
        fam = display(name)
        faces = {s: FontFace(fam, s, p, 'system', 'system') for s, p in found.items() if s in STYLES}
        return FontResolution(None, fam, faces, office_fallback(fam), 'system', 'system', True,
                              install_url=install_url(fam))
    return None


def resolve_local(family: str) -> FontResolution | None:
    """No network: the family from the cache, then the system's installed fonts; None when neither has it."""
    try:
        return _find_local(family, touch=True)
    except Exception:
        return None


@lru_cache(maxsize=512)
def _local_cached(k: str, name: str) -> FontResolution | None:
    try:
        return _find_local(name, touch=False)
    except Exception:
        return None


_ON_CLEAR: list = []      # other modules' font lookups to forget with ours (layout's PDF probe, the thumbnail faces)


def on_clear(fn) -> None:
    """Call fn() whenever clear_caches() runs, so a lookup remembered elsewhere (a family that was missing) is looked
    up again once a download has put it in the cache."""
    if fn not in _ON_CLEAR:
        _ON_CLEAR.append(fn)


def clear_caches() -> None:
    """Forget remembered lookups (after a download, a prune, or when tests move CACHE_DIR)."""
    _local_cached.cache_clear()
    _metrics_of.cache_clear()
    for fn in list(_ON_CLEAR):
        try:
            fn()
        except Exception:
            pass


# ---------- alternatives ----------


def _is_open(family: str) -> bool:
    k = key(family)
    if k in _CATALOG_KEY:
        return True
    meta = _read_meta(k)
    return bool(meta and meta.get('licence') in LICENCES_OK)


def alternative(family: str) -> tuple[str, str] | None:
    """(open family, note) for a family that can't be downloaded; None when `family` is itself open."""
    name = clean(family)
    k = key(name)
    if not k or _is_open(name):
        return None
    alt = _SIMILAR_KEY.get(k)
    if alt is None:
        return None
    shown = display(name)
    if key(alt) in {key(v) for kk, v in METRIC_COMPATIBLE.items() if key(kk) == k}:
        return alt, (f"{shown} isn't openly licensed, so this uses {alt}, an open font with the same letter widths.")
    return alt, f"{shown} isn't openly licensed, so this uses {alt}, the closest open match."


# ---------- metrics and measuring ----------


@lru_cache(maxsize=64)
def _metrics_of(path: str, mtime: float) -> FontMetrics:
    from fontTools.ttLib import TTFont
    font_number = 0
    with TTFont(path, lazy=True, fontNumber=font_number) as f:
        upm = int(f['head'].unitsPerEm) or 1000
        hhea = f['hhea']
        os2 = f['OS/2'] if 'OS/2' in f else None
        asc, desc, gap = int(hhea.ascent), int(hhea.descent), int(hhea.lineGap)
        if os2 is not None and getattr(os2, 'fsSelection', 0) & (1 << 7):   # USE_TYPO_METRICS
            asc, desc, gap = int(os2.sTypoAscender), int(os2.sTypoDescender), int(os2.sTypoLineGap)
        hmtx = f['hmtx']
        cmap = f.getBestCmap() or {}
        widths = {}
        for cp, glyph in cmap.items():
            try:
                widths[cp] = int(hmtx[glyph][0])
            except KeyError:
                continue
        try:
            default = int(hmtx['.notdef'][0])
        except KeyError:
            default = upm // 2

        def glyph_height(ch: str) -> int:
            g = cmap.get(ord(ch))
            if not g:
                return 0
            try:
                if 'glyf' in f:
                    gl = f['glyf'][g]
                    return int(getattr(gl, 'yMax', 0) or 0)
                from fontTools.pens.boundsPen import BoundsPen
                pen = BoundsPen(f.getGlyphSet())
                f.getGlyphSet()[g].draw(pen)
                return int(pen.bounds[3]) if pen.bounds else 0
            except Exception:
                return 0
        xh = int(getattr(os2, 'sxHeight', 0) or 0) if os2 is not None and os2.version >= 2 else 0
        ch = int(getattr(os2, 'sCapHeight', 0) or 0) if os2 is not None and os2.version >= 2 else 0
        xh = xh or glyph_height('x') or round(upm * 0.5)
        ch = ch or glyph_height('H') or round(upm * 0.7)
    return FontMetrics(upm, asc, desc, gap, xh, ch, widths, default)


def metrics(path: str) -> FontMetrics:
    """fontTools metrics of one font file (cached per path)."""
    p = str(path)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    return _metrics_of(p, mtime)


@lru_cache(maxsize=8)
def _builtin_metrics(name: str) -> FontMetrics:
    """Metrics of a PDF standard font from reportlab's AFM tables: what a family nobody has installed is measured as."""
    from reportlab.pdfbase import pdfmetrics
    font = pdfmetrics.getFont(name)
    face = font.face
    widths = {}
    for i, w in enumerate(font.widths):
        if w:
            try:
                cp = ord(bytes([i]).decode('cp1252'))
            except UnicodeDecodeError:
                continue
            widths[cp] = int(w)
    asc = int(getattr(face, 'ascent', 718) or 718)
    desc = int(getattr(face, 'descent', -207) or -207)
    cap = int(getattr(face, 'capHeight', 718) or 718)
    xh = int(getattr(face, 'xHeight', 0) or 0) or round(cap * 0.73)
    avg = round(sum(widths.values()) / max(1, len(widths)))
    return FontMetrics(1000, asc, desc, 0, xh, cap, widths, max(avg, 500))


def _builtin_name(category: str, bold: bool, italic: bool) -> str:
    base = CATEGORY_BUILTIN.get(category, 'Helvetica')
    table = {'Helvetica': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
             'Times-Roman': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic'),
             'Courier': ('Courier', 'Courier-Bold', 'Courier-Oblique', 'Courier-BoldOblique')}[base]
    return table[(1 if bold else 0) + (2 if italic else 0)]


def _style(bold: bool, italic: bool) -> str:
    return 'bolditalic' if bold and italic else 'bold' if bold else 'italic' if italic else 'regular'


def _face_metrics(family: str, bold: bool, italic: bool) -> tuple[FontMetrics, float]:
    """(metrics, width factor) for a family's face: the real file when it is cached or installed (a missing bold face
    is its regular face made 5% wider, as synthetic bold is), else the PDF standard font of its category."""
    name = clean(family)
    res = _local_cached(key(name), name) if name else None
    style = _style(bold, italic)
    if res is not None and res.faces:
        order = {'bolditalic': ('bolditalic', 'bold', 'italic', 'regular'), 'bold': ('bold', 'regular'),
                 'italic': ('italic', 'regular'), 'regular': ('regular',)}[style]
        for s in order:
            face = res.faces.get(s)
            if face is None:
                continue
            try:
                m = metrics(face.path)
            except Exception:
                break
            synthetic = bold and 'bold' not in s
            return m, 1.05 if synthetic else 1.0
    return _builtin_metrics(_builtin_name(category_of(name or 'sans'), bold, italic)), 1.0


def _width(text: str, family: str, size_pt: float, bold: bool, italic: bool) -> float:
    m, factor = _face_metrics(family, bold, italic)
    return m.advance(text, size_pt) * factor


def measure(text: str, family: str, size_pt: float, *, bold: bool = False, italic: bool = False,
            fmt: str = 'pptx') -> float:
    """Width in points. For pptx/docx, the wider of the family and its office_fallback, so text fits either way."""
    text = str(text or '')
    if not text or not size_pt:
        return 0.0
    w = _width(text, family, size_pt, bold, italic)
    if fmt in ('pptx', 'docx', 'xlsx'):
        fb = office_fallback(family)
        if key(fb) != key(family):
            clone = OFFICE_CLONE.get(fb.lower(), fb)
            w = max(w, _width(text, clone, size_pt, bold, italic))
    return w


def wrap(text: str, family: str, size_pt: float, width_pt: float, *, bold: bool = False, italic: bool = False,
         fmt: str = 'pptx') -> list[str]:
    """`text` greedily wrapped to `width_pt` with measure(); a word wider than the line is broken by characters."""
    text = str(text or '')
    if not text.strip():
        return []

    def fits(s: str) -> bool:
        return measure(s, family, size_pt, bold=bold, italic=italic, fmt=fmt) <= width_pt + 1e-6

    lines: list[str] = []
    for para in text.split('\n'):
        words = para.split()
        if not words:
            lines.append('')
            continue
        line = ''
        for word in words:
            cand = f'{line} {word}' if line else word
            if fits(cand):
                line = cand
                continue
            if line:
                lines.append(line)
                line = ''
            if fits(word):
                line = word
                continue
            # a word wider than the line: break it by characters (at least one character per line)
            chunk = ''
            for ch in word:
                if chunk and not fits(chunk + ch):
                    lines.append(chunk)
                    chunk = ch
                else:
                    chunk += ch
            line = chunk
        lines.append(line)
    return lines


def line_metrics(family: str, size_pt: float, line_height: float = 1.2) -> tuple[float, float, float]:
    """(ascent, descent, line advance) in points for a family at a size."""
    m, _ = _face_metrics(family, False, False)
    upm = m.units_per_em or 1000
    return m.ascender * size_pt / upm, abs(m.descender) * size_pt / upm, size_pt * line_height


def x_height(family: str, size_pt: float) -> float:
    """The x-height in points (how big lower-case letters look; legibility compares families by it)."""
    m, _ = _face_metrics(family, False, False)
    return m.x_height * size_pt / (m.units_per_em or 1000)


def subset_for_pdf(path: str, text: str) -> bytes:
    """A TrueType subset of `path` holding only the glyphs `text` needs (plus .notdef and space), for embedding in a
    PDF by hand. reportlab subsets the fonts it embeds itself; this is for painters that embed a file directly."""
    from fontTools import subset
    from fontTools.ttLib import TTFont
    opts = subset.Options()
    opts.name_IDs = ['*']
    opts.notdef_outline = True
    opts.layout_features = ['kern', 'liga']
    opts.drop_tables += ['DSIG']
    with TTFont(str(path), lazy=False) as font:
        sub = subset.Subsetter(opts)
        sub.populate(text=str(text or '') + ' ')
        sub.subset(font)
        buf = io.BytesIO()
        font.save(buf)
    return buf.getvalue()


# ---------- downloads (egress-guarded) ----------


async def _get(http, url: str, *, max_bytes: int) -> bytes:
    """The body of one URL. Every hop passes the egress guard (http(s) only, web ports, public addresses), redirects are
    followed by hand and checked again, and the body is capped at max_bytes while it streams."""
    import aiohttp
    from yarl import URL

    from ..agents import tools
    for _ in range(5):
        try:
            u = tools.check_url(str(url))
            await tools.public_addresses(u.host, u.port)
        except (tools.UrlBlocked, tools.UrlError) as e:
            raise FetchError(f'the address was refused: {e}')
        try:
            async with http.get(u, allow_redirects=False, headers=tools.UA,
                                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as r:
                if r.status in (301, 302, 303, 307, 308):
                    loc = r.headers.get('Location')
                    if not loc:
                        raise FetchError('a redirect without a location')
                    url = str(u.join(URL(loc)))
                    continue
                if r.status >= 400:
                    raise FetchError(f'the server answered {r.status}')
                if r.content_length and r.content_length > max_bytes:
                    raise FetchError('the file is too large')
                body = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        raise FetchError('the file is too large')
                return bytes(body)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f'{type(e).__name__}: {e}')
    raise FetchError('too many redirects')


def normalise_licence(raw) -> str:
    """A source's licence field as an SPDX-style id ('OFL', 'SIL Open Font License 1.1' -> 'OFL-1.1')."""
    if isinstance(raw, dict):
        raw = raw.get('id') or raw.get('type') or raw.get('name') or ''
    s = str(raw or '').strip()
    low = s.lower()
    if re.fullmatch(r'ofl(?:[-_ ]?1\.1)?|sil open font licen[cs]e(?:,? (?:version )?1\.1)?|open font licen[cs]e', low):
        return 'OFL-1.1'
    if re.fullmatch(r'apache(?:[-_ ]?(?:license)?[-_ ]?(?:2(?:\.0)?))?|apache2', low):
        return 'Apache-2.0'
    if re.fullmatch(r'ufl(?:[-_ ]?1\.0)?|ubuntu font licen[cs]e(?:,? (?:version )?1\.0)?', low):
        return 'UFL-1.0'
    return s or 'unknown'


def _check_font(data: bytes) -> None:
    """The bytes are a TrueType font fontTools can read (outlines and metrics present), else FetchError."""
    from fontTools.ttLib import TTFont
    try:
        with TTFont(io.BytesIO(data), lazy=True) as f:
            if 'glyf' not in f or 'hmtx' not in f or 'cmap' not in f:
                raise FetchError('not a TrueType font')
            f['head'].unitsPerEm
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f'the font could not be read ({type(e).__name__})')


def _pick_weight(want: int, weights: list[int]) -> int | None:
    if not weights:
        return None
    if want == 400:
        return 400 if 400 in weights else None
    heavy = [w for w in weights if 600 <= w <= 800]
    return min(heavy, key=lambda w: abs(w - want)) if heavy else None


def _store(family: str, licence: str, source: str, category: str, files: dict[str, bytes], licence_text: str | None,
           extra: dict) -> FontResolution:
    k = key(family)
    d = _family_dir(k)
    names = {}
    for style, data in files.items():
        _atomic_write(d / f'{style}.ttf', data)
        names[style] = f'{style}.ttf'
    text = licence_text or (f'{family} is licensed under {licence}.\nThe full licence: '
                            f'{LICENCE_URL.get(licence, "")}\nSource: {extra.get("page") or source}\n')
    _atomic_write(d / 'LICENSE', text.encode('utf-8'))
    now = time.time()
    meta = {'family': family, 'key': k, 'licence': licence, 'source': source, 'category': category,
            'styles': sorted(names, key=STYLES.index), 'files': names, 'downloaded': now, 'used': now,
            'bytes': sum(len(x) for x in files.values()), **extra}
    _atomic_write(d / 'meta.json', json.dumps(meta, indent=1).encode('utf-8'))   # last: a folder counts once this exists
    clear_caches()
    res = _find_local(family, touch=False)
    if res is None:
        raise FetchError('the downloaded font could not be stored')
    res.source = source
    return res


FS_CATEGORY = {'sans-serif': 'sans', 'serif': 'serif', 'monospace': 'mono', 'display': 'display',
               'handwriting': 'handwriting', 'other': 'display'}


async def _fontsource(family: str, http, styles: tuple[str, ...]) -> FontResolution | None:
    fid = slug(family)
    try:
        meta = json.loads(await _get(http, f'{FONTSOURCE_API}/{fid}', max_bytes=META_BYTES))
    except FetchError:
        return None
    except ValueError:
        return None
    if not isinstance(meta, dict) or key(meta.get('family')) != key(family):
        return None
    name = clean(meta.get('family')) or family
    licence = normalise_licence(meta.get('license'))
    if licence not in LICENCES_OK:     # read before any font file is fetched
        raise LicenceRefused(name, licence)
    subset = str(meta.get('defSubset') or 'latin')
    weights = [int(w) for w in meta.get('weights') or [] if str(w).isdigit()]
    has_italic = 'italic' in (meta.get('styles') or [])
    files: dict[str, bytes] = {}
    for style in ('regular', *[s for s in styles if s != 'regular']):
        want, st = STYLE_VARIANT[style]
        if st == 'italic' and not has_italic:
            continue
        w = _pick_weight(want, weights)
        if w is None:
            continue
        url = None
        try:
            url = meta['variants'][str(w)][st][subset]['url']['ttf']
        except (KeyError, TypeError):
            url = None
        if not isinstance(url, str) or not url.startswith('https://'):
            url = f'{JSDELIVR}/fontsource/fonts/{fid}@latest/{subset}-{w}-{st}.ttf'
        try:
            data = await _get(http, url, max_bytes=MAX_FONT_BYTES)
            _check_font(data)
        except FetchError:
            if style == 'regular':
                return None
            continue
        files[style] = data
    if 'regular' not in files:
        return None
    try:
        lic_text = (await _get(http, f'{JSDELIVR}/npm/@fontsource/{fid}/LICENSE', max_bytes=LICENCE_BYTES)).decode(
            'utf-8', 'replace')
    except FetchError:
        lic_text = None
    return _store(name, licence, 'fontsource', FS_CATEGORY.get(str(meta.get('category')), 'sans'), files, lic_text,
                  {'id': fid, 'subset': subset, 'page': f'https://fontsource.org/fonts/{fid}',
                   'version': meta.get('version')})


GF_LICENCE = {'OFL': 'OFL-1.1', 'APACHE2': 'Apache-2.0', 'UFL': 'UFL-1.0'}
GF_CATEGORY = {'SANS_SERIF': 'sans', 'SERIF': 'serif', 'MONOSPACE': 'mono', 'DISPLAY': 'display',
               'HANDWRITING': 'handwriting'}
GF_LICENCE_FILE = {'ofl': 'OFL.txt', 'apache': 'LICENSE.txt', 'ufl': 'UFL.txt'}


def parse_gf_metadata(text: str) -> dict:
    """The parts of a google/fonts METADATA.pb this needs: name, licence, category and the font files."""
    def one(field_name):
        m = re.search(rf'^{field_name}:\s*"([^"]*)"', text, re.M)
        return m.group(1) if m else None
    fonts = []
    for block in re.finditer(r'^fonts\s*\{(.*?)^\}', text, re.M | re.S):
        b = block.group(1)
        st = re.search(r'style:\s*"(\w+)"', b)
        wt = re.search(r'weight:\s*(\d+)', b)
        fn = re.search(r'filename:\s*"([^"/\\]+)"', b)
        if st and wt and fn:
            fonts.append({'style': st.group(1), 'weight': int(wt.group(1)), 'filename': fn.group(1)})
    return {'name': one('name'), 'license': one('license'), 'category': one('category'), 'fonts': fonts}


async def _google_fonts(family: str, http, styles: tuple[str, ...]) -> FontResolution | None:
    gkey = key(family)
    text = folder = None
    for d in ('ofl', 'apache', 'ufl'):
        try:
            text = (await _get(http, f'{GOOGLE_FONTS}/{d}/{gkey}/METADATA.pb', max_bytes=256 * 1024)).decode(
                'utf-8', 'replace')
            folder = d
            break
        except FetchError:
            continue
    if text is None:
        return None
    md = parse_gf_metadata(text)
    if key(md['name']) != gkey:
        return None
    name = clean(md['name'])
    licence = GF_LICENCE.get(str(md['license'] or '').upper(), normalise_licence(md['license']))
    if licence not in LICENCES_OK:     # read before any font file is fetched
        raise LicenceRefused(name, licence)
    files: dict[str, bytes] = {}
    fetched: dict[str, bytes] = {}
    for style in ('regular', *[s for s in styles if s != 'regular']):
        want, st = STYLE_VARIANT[style]
        cands = [f for f in md['fonts'] if f['style'] == st]
        pick = None
        if cands:
            if want == 400:
                pick = next((f for f in cands if f['weight'] == 400), None)
            else:
                heavy = [f for f in cands if 600 <= f['weight'] <= 800 and '[' not in f['filename']]
                pick = min(heavy, key=lambda f: abs(f['weight'] - want)) if heavy else None
        if pick is None:
            continue
        fname = pick['filename']
        try:
            if fname not in fetched:
                data = await _get(http, f'{GOOGLE_FONTS}/{folder}/{gkey}/{quote(fname)}', max_bytes=MAX_FONT_BYTES)
                _check_font(data)
                fetched[fname] = data
        except FetchError:
            if style == 'regular':
                return None
            continue
        files[style] = fetched[fname]
    if 'regular' not in files:
        return None
    try:
        lic_text = (await _get(http, f'{GOOGLE_FONTS}/{folder}/{gkey}/{GF_LICENCE_FILE[folder]}',
                               max_bytes=LICENCE_BYTES)).decode('utf-8', 'replace')
    except FetchError:
        lic_text = None
    return _store(name, licence, 'google-fonts', GF_CATEGORY.get(str(md['category'] or ''), category_of(name)), files,
                  lic_text, {'id': gkey, 'page': f'https://github.com/google/fonts/tree/main/{folder}/{gkey}'})


def _fallback(requested: str | None, category: str, why: str | None) -> FontResolution:
    """An installed font of the same category: a cached open family, then a system font, then the PDF standard font.
    Never fails."""
    for fam in CATEGORY_OPEN.get(category, CATEGORY_OPEN['sans']):
        res = resolve_local(fam)
        if res is not None and res.source == 'cache':
            break
    else:
        res = None
        for fam in CATEGORY_SYSTEM.get(category, CATEGORY_SYSTEM['sans']):
            res = resolve_local(fam)
            if res is not None:
                break
    if res is None:
        fam = CATEGORY_BUILTIN.get(category, 'Helvetica')
        res = FontResolution(None, fam, {}, OFFICE_FALLBACK.get(category, 'Calibri'), 'builtin', 'builtin', False)
    res.requested = requested
    if requested and why:
        res.note = f'{why}, so this uses {res.family}.'
    return res


_LOCKS: dict[str, asyncio.Lock] = {}


def _session(http):
    """The job's aiohttp session is swapped for a guarded one (its resolver connects only to checked public
    addresses); anything else (tests' fakes) is used as it is. Returns (session, owned)."""
    try:
        import aiohttp
        if isinstance(http, aiohttp.ClientSession):
            from ..create.assets import guarded_session
            return guarded_session(), True
    except Exception:
        pass
    return http, False


async def ensure(family: str, http, *, styles: tuple[str, ...] = STYLES) -> FontResolution:
    """The family ready to use: cache, system, then an open-licensed download (Fontsource via jsDelivr, then the
    google/fonts repository), the licence read from source metadata before download and stored beside the files. A
    proprietary or unknown family resolves to alternative(); offline or http None resolves locally and never raises."""
    requested = clean(family) or None
    try:
        return await _ensure(requested, http, tuple(s for s in styles if s in STYLES) or ('regular',))
    except Exception:
        return _fallback(requested, category_of(requested or 'Inter'), None if requested is None else
                         f'{display(requested)} could not be loaded here')


async def _ensure(requested: str | None, http, styles: tuple[str, ...]) -> FontResolution:
    if requested is None:
        requested_family, note = 'Inter', None
    else:
        requested_family, note = display(requested), None
    local = resolve_local(requested_family)
    if local is not None and local.source in ('user', 'cache'):
        local.requested = requested
        return local
    alt = alternative(requested_family)
    target = requested_family
    if alt is not None and (local is None or local.source != 'system'):
        target, note = alt
        local = resolve_local(target)
    if local is not None:
        local.requested = requested
        local.note = note
        return local
    if http is None:
        res = _fallback(requested, category_of(target),
                        f"{target} isn't installed and can't be downloaded here (offline)")
        if note:
            res.note = f'{note} {res.note}' if res.note else note
        return res
    k = key(target)
    lock = _LOCKS.setdefault(k, asyncio.Lock())
    async with lock:
        local = resolve_local(target)
        if local is None:
            session, owned = _session(http)
            try:
                try:
                    local = await _fontsource(target, session, styles)
                    if local is None:
                        local = await _google_fonts(target, session, styles)
                except LicenceRefused as e:
                    res = _fallback(requested, category_of(target),
                                    f"{e.family} uses the {e.licence} licence, which isn't an open font licence, so "
                                    f"it wasn't downloaded")
                    return res
                except FetchError:
                    local = None
            finally:
                if owned:
                    await session.close()
            if local is not None:
                try:
                    prune(CACHE_BYTES, keep=(k,))
                except Exception:
                    pass
    if local is None:
        why = (f"{target} isn't available as an open font" if target == requested_family else
               f"{target} couldn't be downloaded")
        res = _fallback(requested, category_of(target), why)
        if note:
            res.note = f'{note} {res.note}' if res.note else note
        return res
    local.requested = requested
    local.note = note
    return local


# ---------- search, previews, pruning ----------

_INDEX: dict = {'at': 0.0, 'fonts': None}


def _fs_styles(item: dict) -> list[str]:
    weights = [int(w) for w in item.get('weights') or [] if str(w).isdigit()]
    italic = 'italic' in (item.get('styles') or [])
    out = []
    if 400 in weights:
        out.append('regular')
    if any(600 <= w <= 800 for w in weights):
        out.append('bold')
    if italic and 400 in weights:
        out.append('italic')
    if italic and any(600 <= w <= 800 for w in weights):
        out.append('bolditalic')
    return out


async def _index(http) -> list[dict]:
    """Fontsource's family list (memory, then data/cache/fonts/_index.json for a week, then the API)."""
    now = time.time()
    if _INDEX['fonts'] is not None and now - _INDEX['at'] < INDEX_TTL:
        return _INDEX['fonts']
    disk = Path(CACHE_DIR) / '_index.json'
    try:
        if now - disk.stat().st_mtime < INDEX_TTL:
            fonts = json.loads(disk.read_text('utf-8'))
            if isinstance(fonts, list):
                _INDEX.update(at=now, fonts=fonts)
                return fonts
    except (OSError, ValueError):
        pass
    session, owned = _session(http)
    try:
        raw = json.loads(await _get(session, FONTSOURCE_API, max_bytes=META_BYTES))
    finally:
        if owned:
            await session.close()
    if not isinstance(raw, list):
        raise FetchError('the font list was not a list')
    fonts = [{'family': clean(x.get('family')), 'category': FS_CATEGORY.get(str(x.get('category'))),
              'licence': normalise_licence(x.get('license')), 'styles': _fs_styles(x)}
             for x in raw if isinstance(x, dict) and x.get('family') and x.get('category') != 'icons']
    fonts = [f for f in fonts if f['category'] and f['licence'] in LICENCES_OK and f['styles']]
    _INDEX.update(at=now, fonts=fonts)
    try:
        _atomic_write(disk, json.dumps(fonts).encode('utf-8'))
    except OSError:
        pass
    return fonts


def _info(family: str, category: str, licence: str, styles: list[str], source: str) -> FontInfo:
    local = _local_cached(key(family), family)
    return FontInfo(family=family, category=category, licence=licence, source=local.source if local else source,
                    styles=list(styles), installed=local is not None,
                    preview=f'/api/fonts/preview?family={quote(family)}')


def _offline_list() -> list[dict]:
    out = [{'family': f, 'category': c, 'licence': lic, 'styles': list(STYLES)} for f, (c, lic) in CATALOG.items()]
    seen = {key(f['family']) for f in out}
    try:
        for d in sorted(Path(CACHE_DIR).iterdir()) if Path(CACHE_DIR).is_dir() else []:
            meta = _read_meta(d.name) if d.is_dir() else None
            if meta and meta.get('licence') in LICENCES_OK and key(meta.get('family')) not in seen:
                out.append({'family': meta['family'], 'category': meta.get('category') or 'sans',
                            'licence': meta['licence'], 'styles': list(meta.get('styles') or ['regular'])})
    except OSError:
        pass
    return out


def _rank(fonts: list[dict], q: str, limit: int) -> list[dict]:
    qk = key(q)
    if not qk:
        return fonts[:limit]
    alt = _SIMILAR_KEY.get(qk)
    popular = {key(f): i for i, f in enumerate(CATALOG)}
    scored = []
    for f in fonts:
        fk = key(f['family'])
        if alt and fk == key(alt):
            score = 0
        elif fk == qk:
            score = 1
        elif fk.startswith(qk):
            score = 2
        elif qk in fk:
            score = 3
        else:
            continue
        scored.append((score, popular.get(fk, 9999), f['family'].lower(), f))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:limit]]


async def search(q: str, http, limit: int = 20) -> tuple[list[FontInfo], bool]:
    """(open-licensed families matching q, offline). Empty q: a curated popular list. Never returns non-open fonts."""
    q = clean(q)[:64]
    limit = max(1, min(50, int(limit or 20)))
    offline = http is None
    fonts = None
    if not offline:
        try:
            fonts = await _index(http)
        except Exception:
            offline, fonts = True, None
    if not q or fonts is None:
        pool = _offline_list()
        if fonts is not None:   # online and no query: the curated list, with Fontsource's styles
            by_key = {key(f['family']): f for f in fonts}
            pool = [by_key.get(key(p['family']), p) for p in pool]
    else:
        pool = fonts
    picked = _rank([f for f in pool if f.get('licence') in LICENCES_OK], q, limit)
    return [_info(f['family'], f.get('category') or 'sans', f['licence'], f.get('styles') or ['regular'],
                  'fontsource' if not offline else 'cache') for f in picked], offline


def preview_png(family: str, text: str = 'The quick brown fox', px: int = 32) -> bytes | None:
    """A PNG sample of a cached or installed open family (GET /api/fonts/preview); None when unavailable."""
    res = resolve_local(family)
    if res is None or 'regular' not in res.faces:
        return None
    if res.source == 'system' and not _is_open(res.family):
        return None   # an installed font whose licence isn't known to be open is not shown
    try:
        from PIL import Image, ImageDraw, ImageFont
        px = max(8, min(96, int(px)))
        text = clean(text)[:60] or res.family
        font = ImageFont.truetype(res.faces['regular'].path, px)
        width = int(font.getlength(text)) + px
        asc, desc = font.getmetrics()
        img = Image.new('RGB', (max(width, px * 2), asc + desc + px // 2), (255, 255, 255))
        ImageDraw.Draw(img).text((px // 2, px // 4), text, font=font, fill=(17, 24, 39))
        buf = io.BytesIO()
        img.save(buf, 'PNG', optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _dir_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def prune(cap_bytes: int = CACHE_BYTES, keep: tuple[str, ...] = ()) -> int:
    """LRU-evict cached families above the cap; returns bytes freed."""
    root = Path(CACHE_DIR)
    if not root.is_dir():
        return 0
    fams = []
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith(('_', '.')):
            continue
        try:
            used = (d / 'meta.json').stat().st_mtime
        except OSError:
            used = 0.0   # a folder without meta.json is a broken download: evicted first
        fams.append((used, d, _dir_bytes(d)))
    total = sum(b for _, _, b in fams)
    freed = 0
    for used, d, size in sorted(fams, key=lambda x: x[0]):
        if total <= cap_bytes:
            break
        if d.name in keep:
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size
        freed += size
    if freed:
        clear_caches()
    return freed
