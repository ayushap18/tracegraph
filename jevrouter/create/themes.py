"""The file themes (F6, A4): one font pair and one palette per file, every text colour at least 4.5:1 against the
background it sits on. `dark` has a light `paper` variant for DOCX and XLSX, which are read on white pages. `mono` is
black on white with greys only (docs/PLAN-accuracy-v2.md C6): its charts use hatch patterns and dashes, never colour."""

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
        'font': 'Calibri', 'heading_font': 'Calibri', 'mono': 'Courier New', 'patterns': True,
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


def worst_contrast(name: str | None) -> tuple[float, str]:
    """The lowest text contrast in a theme (both its screen and paper variants) and which pair it is."""
    worst = (99.0, '')
    for paper in (False, True):
        t = get(name, paper)
        for fg, bg in TEXT_PAIRS:
            c = contrast(t[fg], t[bg])
            if c < worst[0]:
                worst = (c, f'{fg} on {bg}')
    return worst
