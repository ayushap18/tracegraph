"""The file themes (F6, A4): one font pair and one palette per file, every text colour at least 4.5:1 against the
background it sits on. `dark` has a light `paper` variant for DOCX and XLSX, which are read on white pages. `mono` is
black on white with greys only (docs/PLAN-accuracy-v2.md C6): its charts use hatch patterns and dashes, never colour.

A design file (create/design.py) becomes a theme dict of its own (`from_design`), with the same keys, every text pair
repaired to 4.5:1 and chart colours to 3:1. `resolve(spec)` is the one call renderers and rules make
(docs/PLAN-files-robust.md 4.5). `hatch` means charts use patterns; `grey_images` means pictures are greyscaled.
"""
import colorsys

THEMES = {
    'clean': {
        'bg': 'FFFFFF', 'text': '1F2328', 'muted': '57606A', 'heading': '0B3D91', 'accent': '1D4ED8',
        'header_bg': '1E3A8A', 'header_text': 'FFFFFF', 'stripe': 'F3F6FA', 'code_bg': 'F4F4F5', 'border': 'D0D7DE',
        'palette': ['2563EB', 'F59E0B', '059669', 'DC2626', '7C3AED', '0891B2'],
        'pdf_font': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
        'font': 'Calibri', 'heading_font': 'Calibri', 'mono': 'Courier New',
    },
    'dark': {
        'bg': '111827', 'text': 'F3F4F6', 'muted': 'A1A8B3', 'heading': '93C5FD', 'accent': '60A5FA',
        'header_bg': '1E3A8A', 'header_text': 'FFFFFF', 'stripe': '1B2433', 'code_bg': '1F2937', 'border': '374151',
        'palette': ['60A5FA', 'FBBF24', '34D399', 'F87171', 'A78BFA', '22D3EE'],
        'pdf_font': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
        'font': 'Calibri', 'heading_font': 'Calibri', 'mono': 'Courier New',
        'paper': {'bg': 'FFFFFF', 'text': '111827', 'muted': '4B5563', 'heading': '1E3A8A', 'accent': '1D4ED8',
                  'header_bg': '111827', 'header_text': 'FFFFFF', 'stripe': 'F3F4F6', 'code_bg': 'F3F4F6',
                  'border': 'D1D5DB', 'palette': ['1D4ED8', 'D97706', '059669', 'DC2626', '7C3AED', '0891B2']},
    },
    'warm': {
        'bg': 'FFFBF5', 'text': '2B2118', 'muted': '6B5B4B', 'heading': '7C2D12', 'accent': '9A3412',
        'header_bg': '7C2D12', 'header_text': 'FFFFFF', 'stripe': 'FAF0E4', 'code_bg': 'F5EDE3', 'border': 'E7D8C5',
        'palette': ['C2410C', '0F766E', 'CA8A04', '7C3AED', 'BE123C', '2563EB'],
        'pdf_font': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic'),
        'font': 'Georgia', 'heading_font': 'Georgia', 'mono': 'Courier New',
    },
    'mono': {
        'bg': 'FFFFFF', 'text': '000000', 'muted': '4D4D4D', 'heading': '000000', 'accent': '1A1A1A',
        'header_bg': '000000', 'header_text': 'FFFFFF', 'stripe': 'F2F2F2', 'code_bg': 'F2F2F2', 'border': '8C8C8C',
        'palette': ['1A1A1A', '595959', '8C8C8C', 'BFBFBF', '404040', 'A6A6A6'],
        'pdf_font': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
        'font': 'Calibri', 'heading_font': 'Calibri', 'mono': 'Courier New', 'patterns': True, 'hatch': True,
        'grey_images': True,
    },
}

# Text colour -> the backgrounds it is drawn on. Every pair must reach 4.5:1 (A4).
TEXT_PAIRS = [('text', 'bg'), ('muted', 'bg'), ('heading', 'bg'), ('accent', 'bg'), ('header_text', 'header_bg'),
              ('text', 'stripe'), ('text', 'code_bg')]


def get(name: str | None, paper: bool = False) -> dict:
    """A theme by name (unknown names fall back to clean); paper=True gives the variant for white pages."""
    t = THEMES.get(name or 'clean') or THEMES['clean']
    return {**t, **t['paper']} if paper and 'paper' in t else t


def _lum(hex_color: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two RRGGBB colours."""
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def worst_contrast(name) -> tuple[float, str]:
    """The lowest text contrast in a theme (both its screen and paper variants, or one theme dict) and which pair."""
    worst = (99.0, '')
    variants = [name] if isinstance(name, dict) else [get(name, p) for p in (False, True)]
    for t in variants:
        for fg, bg in TEXT_PAIRS:
            c = contrast(t[fg], t[bg])
            if c < worst[0]:
                worst = (c, f'{fg} on {bg}')
    return worst


# ---------- design files (docs/PLAN-files-robust.md 4.5) ----------

PDF_BUILTIN = {'sans': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
               'serif': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic'),
               'mono': ('Courier', 'Courier-Bold', 'Courier-Oblique', 'Courier-BoldOblique')}
ROLE_NAMES = {'text': 'Body text', 'muted': 'Muted text', 'heading': 'Heading text', 'accent': 'The accent colour',
              'header_text': 'Table header text', 'palette': 'Chart colours'}


def clean_design(d) -> dict | None:
    """design.clean_design, here so normalize can import it with the themes."""
    from .design import clean_design as _clean
    return _clean(d)


def _hls(h: str) -> tuple[float, float, float]:
    return colorsys.rgb_to_hls(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def _from_hls(h: float, l: float, s: float) -> str:
    return ''.join(f'{max(0, min(255, round(c * 255))):02X}' for c in colorsys.hls_to_rgb(h, l, s))


def mix(a: str, b: str, t: float) -> str:
    """a moved t of the way to b."""
    ra = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    rb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return ''.join(f'{max(0, min(255, round(x + (y - x) * t))):02X}' for x, y in zip(ra, rb))


def nudge(fg: str, bg: str, ratio: float = 4.5) -> str:
    """fg with its lightness moved away from bg in steps of 0.02 until it reaches `ratio` against bg (unchanged when
    it already does). Falls back to black or white."""
    if contrast(fg, bg) >= ratio:
        return fg
    h, l, s = _hls(fg)
    step = -0.02 if _lum(bg) >= 0.18 else 0.02
    for _ in range(60):
        l = max(0.0, min(1.0, l + step))
        cand = _from_hls(h, l, s)
        if contrast(cand, bg) >= ratio:
            return cand
        if l in (0.0, 1.0):
            break
    return max(('000000', 'FFFFFF'), key=lambda c: contrast(c, bg))


def fit(fg: str, backs, ratio: float = 4.5) -> str:
    """fg changed as little as possible (its lightness moved in steps of 0.02, the nearer direction first, then black or
    white) so it reaches `ratio` against every background in `backs` at once. `backs` is a list, or a function of
    the candidate colour (for backgrounds derived from the text colour itself). Never returns a colour whose worst
    contrast is lower than fg's own; when no candidate reaches the ratio, the one with the best worst contrast."""
    def worst(c):
        bs = backs(c) if callable(backs) else backs
        return min((contrast(c, b) for b in bs), default=99.0)
    best, best_score = fg, worst(fg)
    if best_score >= ratio:
        return fg
    h, l, s = _hls(fg)
    bs0 = backs(fg) if callable(backs) else backs
    first = -1 if bs0 and sum(_lum(b) for b in bs0) / len(bs0) >= 0.18 else 1
    for k in range(1, 51):
        for d in (first, -first):
            lk = l + d * 0.02 * k
            if not 0.0 <= lk <= 1.0:
                continue
            cand = _from_hls(h, lk, s)
            sc = worst(cand)
            if sc >= ratio:
                return cand
            if sc > best_score:
                best, best_score = cand, sc
    for cand in ('000000', 'FFFFFF'):
        sc = worst(cand)
        if sc >= ratio:
            return cand
        if sc > best_score:
            best, best_score = cand, sc
    return best


def _tint(bg: str, text: str, t: float, ratio: float = 4.5) -> str:
    """A stripe or code background: bg moved t toward the text colour, or away from it (toward black or white) when
    moving toward it would leave the text below `ratio`."""
    toward = mix(bg, text, t)
    if contrast(text, toward) >= ratio or contrast(text, toward) >= contrast(text, bg):
        return toward
    return mix(bg, '000000' if _lum(text) > _lum(bg) else 'FFFFFF', t)


def base_for(design: dict) -> str:
    """The built-in theme a design starts from: 'dark' on a dark background, 'warm' when red exceeds blue by 6+."""
    bg = ((design or {}).get('colors') or {}).get('bg') or 'FFFFFF'
    if _lum(bg) < 0.2:
        return 'dark'
    return 'warm' if int(bg[0:2], 16) - int(bg[4:6], 16) >= 6 else 'clean'


def _office_font(spec: dict | None, fallback: str) -> str:
    if not spec:
        return fallback
    from . import fonts
    return fonts.office_name([spec.get('family'), *(spec.get('fallbacks') or [])], spec.get('category')) or fallback


def from_design(design: dict, paper: bool = False) -> dict:
    """The theme dict for a design (every key the renderers read), with contrast repaired. `paper` changes nothing:
    the design's own background is what it asked for on every page."""
    base = THEMES[base_for(design)]
    base = {k: v for k, v in base.items() if k != 'paper'}
    c = dict(design.get('colors') or {})
    t = dict(base)
    bg = c.get('bg') or ('FFFFFF' if base_for(design) != 'dark' else base['bg'])
    t['bg'] = bg
    given = {k: v for k, v in c.items() if isinstance(v, str)}
    t['text'] = fit(c.get('text') or base['text'], [bg])
    t['heading'] = c.get('heading') or t['text']
    t['muted'] = c.get('muted') or mix(t['text'], bg, 0.35)
    t['accent'] = c.get('accent') or next(iter(p for p in design.get('palette') or [] if not design.get('hatch')),
                                          t['heading'])
    t['border'] = c.get('border') or mix(bg, t['text'], 0.15)
    t['surface'] = c.get('surface') or bg
    t['header_bg'] = c.get('header_bg') or t['heading']
    hdr = c.get('header_text')
    t['header_text'] = hdr or max((bg, 'FFFFFF', '000000'), key=lambda x: contrast(x, t['header_bg']))

    def text_backs(x):  # stripe and code backgrounds follow the text colour unless the design set them
        return [bg, c.get('stripe') or _tint(bg, x, 0.04), c.get('code_bg') or _tint(bg, x, 0.06)]
    t['text'] = fit(t['text'], text_backs)
    t['stripe'] = c.get('stripe') or _tint(bg, t['text'], 0.04)
    t['code_bg'] = c.get('code_bg') or _tint(bg, t['text'], 0.06)
    t['hatch'] = bool(design.get('hatch'))
    t['patterns'] = t['hatch']
    t['grey_images'] = False
    t['radius'] = design.get('radius') if design.get('radius') is not None else 3
    body = design.get('body_font') or design.get('heading_font')
    head = design.get('heading_font') or body
    mono = design.get('mono_font')
    cat = (body or {}).get('category') or 'sans'
    t['pdf_font'] = PDF_BUILTIN.get(cat, PDF_BUILTIN['sans'])
    # no font in the design: the plain sans of the clean theme, never the warm theme's serif picked by the background
    t['font'] = _office_font(body, THEMES['clean']['font'] if cat != 'serif' else 'Georgia')
    t['heading_font'] = _office_font(head, t['font'])
    t['mono'] = _office_font(mono, 'Consolas') if mono else 'Consolas'
    if body:
        t['font_stack'] = [body['family'], *(body.get('fallbacks') or [])]
        t['font_category'] = cat
    t['design'] = design.get('name') or 'design.md'
    # contrast repair: each text colour once against all of its backgrounds, chart colours to 3:1 on the background
    backs: dict[str, list] = {}
    for fg, back in TEXT_PAIRS:
        backs.setdefault(fg, []).append(back)
    for fg, names in backs.items():
        t[fg] = fit(t[fg], [t[b] for b in names])
    nudged, notes = [], []
    for fg in backs:
        orig = given.get(fg) or (base['text'] if fg == 'text' else None)
        if orig and t[fg] != orig:
            nudged.append(fg)
    pal = [p for p in (design.get('palette') or []) if isinstance(p, str) and len(p) == 6] or list(base['palette'])
    fixed_pal = [nudge(p, bg, 3.0) for p in pal]
    if fixed_pal != pal and not t['hatch']:  # hatched charts are told apart by pattern, not fill
        nudged.append('palette')
    t['palette'] = fixed_pal
    for role in nudged:
        if role != 'palette' and role not in c:
            continue  # a colour the design did not set was derived; no note
        what = ROLE_NAMES.get(role, role.replace('_', ' ').capitalize())
        if role == 'palette':
            lighter = sum(_hls(a)[1] for a in fixed_pal) >= sum(_hls(a)[1] for a in pal)
            notes.append(f'{what} were {"lightened" if lighter else "darkened"} slightly to stay visible on the '
                         f'background.')
        else:
            dl = _hls(t[role])[1] - _hls(c[role])[1]
            how = 'changed' if abs(dl) > 0.4 else ('lightened slightly' if dl > 0 else 'darkened slightly')
            notes.append(f'{what} was {how} to stay readable.')
    if not body and not mono:
        notes.append(f"The design's fonts could not be read, so the file uses {t['font']}.")
    t['nudged'] = nudged
    t['design_notes'] = notes
    return t


def resolve(spec: dict | None, paper: bool = False) -> dict:
    """The theme dict renderers and rules use: the design's when the spec carries one, else the named theme."""
    spec = spec if isinstance(spec, dict) else {}
    design = spec.get('design')
    if isinstance(design, dict) and design.get('colors'):
        d = clean_design(design)
        if d:
            return from_design(d, paper)
    return get(spec.get('theme'), paper)
