"""The body font a file uses (docs/PLAN-accuracy-v2.md C6): TRACEGRAPH_BODY_FONT, then an installed system font that
matches the request, then an open-licensed family Studio's font manager has cached (studio/fonts.py; this module is the
legacy adapter over it and never downloads), then the theme's built-in font.

A PDF embeds the TrueType font it uses (subset). Word, PowerPoint and Excel files only name a font: they show it where it
is installed. The note says so plainly whenever a requested font is not what the file uses, and makes no claim about the
licence of the font that was named.
"""
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ENV = 'TRACEGRAPH_BODY_FONT'
FONT_DIRS = ('/System/Library/Fonts', '/Library/Fonts', '~/Library/Fonts', '/usr/share/fonts', '/usr/local/share/fonts',
             '~/.fonts', '~/.local/share/fonts', 'C:\\Windows\\Fonts')
BUILTIN = {'pdf': 'Helvetica', 'docx': 'Calibri', 'pptx': 'Calibri', 'xlsx': 'Calibri', 'md': None}
# PDF standard fonts that need no file: a request for one of them is met as asked.
PDF_STANDARD = {'helvetica': 'Helvetica', 'arial': None, 'times': 'Times-Roman', 'times new roman': None,
                'times roman': 'Times-Roman', 'courier': 'Courier'}
UPPER = {'ibm': 'IBM', 'ui': 'UI', 'pt': 'PT', 'ms': 'MS'}
BOLD = re.compile(r'[-_ ]?(?:bold|bd|b)$', re.I)
ITALIC = re.compile(r'[-_ ]?(?:italic|oblique|it|i)$', re.I)


@dataclass
class FontChoice:
    requested: str | None
    used: str
    regular: str | None
    bold: str | None
    italic: str | None
    embedded: bool
    note: str | None


def display(name: str | None) -> str:
    """'anthropic sans' -> 'Anthropic Sans'; 'ibm plex sans' -> 'IBM Plex Sans'."""
    return ' '.join(UPPER.get(w, w[:1].upper() + w[1:]) for w in str(name or '').split())


def key(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def family_of(path: str) -> str:
    """A TrueType font's family name from its name table, else the file stem."""
    try:
        from reportlab.pdfbase.ttfonts import TTFontFile
        fam = TTFontFile(path).familyName
        fam = fam.decode('utf-8', 'replace') if isinstance(fam, bytes) else str(fam)
        if fam.strip():
            return fam.strip()
    except Exception:
        pass
    return Path(path).stem


def _style_of(stem: str) -> tuple[str, str]:
    """(family key, style) from a file stem: 'Arial Bold Italic' -> ('arial', 'bolditalic')."""
    k = key(stem)
    for suffix, style in (('bolditalic', 'bolditalic'), ('boldoblique', 'bolditalic'), ('bi', 'bolditalic'),
                          ('bold', 'bold'), ('bd', 'bold'), ('italic', 'italic'), ('oblique', 'italic'),
                          ('it', 'italic'), ('regular', 'regular'), ('roman', 'regular'), ('book', 'regular')):
        if k.endswith(suffix) and len(k) > len(suffix) + 2:
            return k[:-len(suffix)], style
    return k, 'regular'


# style suffixes that are also the end of family names: Times New Roman, Bookman Old Style Book, Sanskrit
AMBIGUOUS_SUFFIX = re.compile(r'(?:roman|book|it|bi|bd)$')


@lru_cache(maxsize=1)
def system_index() -> dict[str, dict[str, str]]:
    """{family key: {style: path}} for the TrueType files in the usual font folders, built once. Only .ttf files:
    reportlab embeds TrueType outlines, not .ttc collections or CFF .otf files."""
    out: dict[str, dict[str, str]] = {}
    for d in FONT_DIRS:
        root = Path(os.path.expanduser(d))
        if not root.is_dir():
            continue
        try:
            files = [p for p in root.rglob('*') if p.suffix.lower() == '.ttf']
        except OSError:
            continue
        for p in files[:5000]:
            fam, style = _style_of(p.stem)
            out.setdefault(fam, {}).setdefault(style, str(p))
            if key(p.stem) != fam and AMBIGUOUS_SUFFIX.search(key(p.stem)):
                # "Times New Roman.ttf", "Sanskrit.ttf": the suffix may be part of the family name, so the file is
                # also the regular face of the whole stem
                out.setdefault(key(p.stem), {}).setdefault('regular', str(p))
    return out


def _env_font() -> tuple[str, str | None, str | None, str] | None:
    path = os.environ.get(ENV, '').strip()
    if not path or not os.path.isfile(path):
        return None
    bold = os.environ.get(ENV + '_BOLD', '').strip() or None
    italic = os.environ.get(ENV + '_ITALIC', '').strip() or None
    return (path, bold if bold and os.path.isfile(bold) else None, italic if italic and os.path.isfile(italic) else None,
            family_of(path))


def env_family() -> str | None:
    """The TRACEGRAPH_BODY_FONT family, or None (the config event's `fonts.body`)."""
    env = _env_font()
    return env[3] if env else None


def matches(requested: str, family: str) -> bool:
    a, b = key(requested), key(family)
    return bool(a) and (a == b or b.startswith(a) or a.startswith(b) and len(b) >= 4)


# Fonts every copy of Office has: a Word, PowerPoint or Excel file names one of these (or the env font), never a brand
# font a reader is unlikely to have (docs/PLAN-files-robust.md 4.5).
OFFICE = ('Calibri', 'Arial', 'Segoe UI', 'Georgia', 'Cambria', 'Times New Roman', 'Consolas', 'Courier New')
CATEGORY_OFFICE = {'sans': 'Calibri', 'serif': 'Georgia', 'mono': 'Consolas'}
CATEGORY_PDF = {'sans': 'Helvetica', 'serif': 'Times-Roman', 'mono': 'Courier'}
GENERIC = {'serif', 'sans-serif', 'sans', 'monospace', 'mono', 'ui-sans-serif', 'ui-serif', 'ui-monospace', 'system-ui',
           'ui-rounded', 'cursive', 'fantasy', '-apple-system', 'blinkmacsystemfont', 'emoji', 'math'}


def office_name(stack, category: str | None = None) -> str | None:
    """The first family of a font stack that Office ships with (or the env font), else the category's default."""
    env = env_family()
    for name in stack or []:
        if not name:
            continue
        if env and matches(str(name), env):
            return env
        hit = next((o for o in OFFICE if key(o) == key(str(name))), None)
        if hit:
            return hit
    return CATEGORY_OFFICE.get(category or '', None) if category else None


def builtin(fmt: str, theme=None, category: str | None = None) -> str | None:
    if isinstance(theme, dict):
        category = category or theme.get('font_category') or ('serif' if theme.get('font') == 'Georgia' else None)
        theme = None
    if fmt == 'pdf':
        return CATEGORY_PDF.get(category or '', 'Times-Roman' if theme == 'warm' else 'Helvetica')
    if fmt == 'md':
        return None
    return CATEGORY_OFFICE.get(category or '', 'Georgia' if theme == 'warm' else 'Calibri')


def studio_cached(name: str) -> tuple[str, str, str | None, str | None] | None:
    """(family, regular, bold, italic) TrueType paths of an open-licensed family Studio's font manager has already
    downloaded (studio/fonts.py, data/cache/fonts), or None. No network: a font is only ever downloaded by Studio."""
    try:
        from ..studio import fonts as studio_fonts
        res = studio_fonts.resolve_local(name)
    except Exception:
        return None
    if res is None or res.source != 'cache' or not res.embeddable:
        return None
    faces = res.faces
    path = lambda s: faces[s].path if s in faces and faces[s].path.lower().endswith('.ttf') else None  # noqa: E731
    return res.family, faces['regular'].path, path('bold'), path('italic')


def resolve(requested: str | None, fmt: str, theme=None, stack=None, category: str | None = None) -> FontChoice:
    """The font a file in `fmt` uses for body text, and a plain note when it is not the one requested. Precedence:
    the font the request names, then the design's stack (`stack`, or the theme dict's `font_stack`), then
    TRACEGRAPH_BODY_FONT, then the built-in. `theme` is a theme name or a theme dict."""
    if isinstance(theme, dict):
        stack = stack if stack is not None else theme.get('font_stack')
        category = category or theme.get('font_category')
    req = ' '.join(str(requested or '').lower().split()) or None
    if req is None and stack:
        return _from_stack([str(x) for x in stack if x], fmt, theme, category)
    shown = display(req) if req else None
    fallback = builtin(fmt, theme, category)
    if fmt == 'md':
        note = (f'You asked for {shown}; a Markdown file carries no font, so it shows in the reader\'s font.'
                if req else None)
        return FontChoice(req, '', None, None, None, False, note)
    env = _env_font()
    if fmt == 'pdf':
        if env and (req is None or matches(req, env[3])):
            return FontChoice(req, env[3], env[0], env[1], env[2], True, None)
        if req:
            std = PDF_STANDARD.get(req)
            if std and std == fallback:
                return FontChoice(req, std, None, None, None, False, None)
            found = system_index().get(key(req))
            if found and found.get('regular'):
                return FontChoice(req, display(req), found['regular'], found.get('bold'), found.get('italic'), True,
                                  None)
            cached = studio_cached(req)
            if cached is not None:
                return FontChoice(req, cached[0], cached[1], cached[2], cached[3], True, None)
            if std:
                return FontChoice(req, std, None, None, None, False, None)
            return FontChoice(req, fallback, None, None, None, False,
                              f'You asked for {shown}; it isn\'t available to embed here, so the PDF uses {fallback}. '
                              f'To use a font you\'re licensed for, set {ENV} to its .ttf file and convert the file '
                              f'again (0 tokens).')
        return FontChoice(None, fallback, None, None, None, False, None)
    # Word, PowerPoint and Excel name the font; the reader's machine draws it
    label = {'docx': 'Word', 'pptx': 'PowerPoint', 'xlsx': 'Excel'}.get(fmt, fmt)
    if req:
        return FontChoice(req, shown, None, None, None, False,
                          f'The {label} file names {shown} (named, not embedded; it shows only where the font is '
                          f'installed).')
    if env:
        return FontChoice(None, env[3], None, None, None, False, None)
    return FontChoice(None, fallback, None, None, None, False, None)


def _from_stack(stack: list[str], fmt: str, theme, category: str | None) -> FontChoice:
    """A design's font stack: PDF embeds the first installed static TrueType font, else the category's built-in;
    Word, PowerPoint and Excel name the first family Office ships with, else the category's default."""
    family = stack[0] if stack else ''
    category = category or 'sans'
    if fmt == 'md':
        return FontChoice(None, '', None, None, None, False,
                          'A Markdown file carries no colours or fonts; convert it to PDF, Word or PowerPoint to see '
                          'the design (0 tokens).')
    env = _env_font()
    if fmt == 'pdf':
        used = None
        for name in stack:
            if key(name) in {key(g) for g in GENERIC}:
                continue
            if env and matches(name, env[3]):
                used = FontChoice(None, env[3], env[0], env[1], env[2], True, None)
                break
            found = system_index().get(key(name))
            if found and found.get('regular'):
                used = FontChoice(None, display(name) if name.islower() else name, found['regular'],
                                  found.get('bold'), found.get('italic'), True, None)
                break
            cached = studio_cached(name)
            if cached is not None:
                used = FontChoice(None, cached[0], cached[1], cached[2], cached[3], True, None)
                break
        if used is None and env:
            used = FontChoice(None, env[3], env[0], env[1], env[2], True, None)
        if used is None:
            used = FontChoice(None, builtin('pdf', None, category), None, None, None, False, None)
    else:
        name = office_name(stack, None)
        if name is None and env:
            name = env[3]
        used = FontChoice(None, name or builtin(fmt, None, category), None, None, None, False, None)
    if family and key(used.used) != key(family):
        used.note = f"The design's font {family} isn't available here, so the file uses {used.used}."
    return used
