"""The 8 presets, the student templates and prompt-word mapping (docs/PLAN-designer.md 3.2, 4 and 9.3).

Owner: builder F. PRESETS, TEMPLATES, LEGACY_THEME and PROMPT_WORDS are contract data.
"""
from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass

from .tokens import DesignSystem

PRESETS = ('bold-dark', 'editorial', 'minimal', 'vibrant', 'pastel', 'academic', 'mono', 'high-legibility')
DEFAULT_PRESET = 'minimal'
# the legacy theme a spec names -> the preset Studio uses for it (so `theme: dark` stays dark)
LEGACY_THEME = {'clean': 'minimal', 'dark': 'bold-dark', 'warm': 'editorial', 'mono': 'mono'}
# a preset -> the legacy theme recorded in CreatedFile.theme (V8 and old clients read it)
THEME_OF = {'bold-dark': 'dark', 'editorial': 'warm', 'minimal': 'clean', 'vibrant': 'clean', 'pastel': 'clean',
            'academic': 'clean', 'mono': 'mono', 'high-legibility': 'clean'}

# Prompt words -> effect, applied in this order of strength (docs/PLAN-designer.md 9.3):
#   1. an exact preset id or name ("bold-dark", "high legibility")      -> that preset
#   2. accessibility and print words (mono, high-legibility)             -> those presets always win over mood words
#   3. the first mood word in the text                                   -> its preset
#   4. dark / light words                                                -> dark override on whatever preset was chosen
#   5. font names (create/brief.font_of)                                 -> families override (body, or the role named)
# Each entry: (id, regex source, effect). Effects: {'preset': id} | {'dark': bool} | {'mood': word} | {'print': True}
PROMPT_WORDS: list[tuple[str, str, dict]] = [
    ('mono', r'\bblack[\s-]*(?:and|&)[\s-]*white\b|\bmonochrome\b|\bgr[ae]y[\s-]?scale\b|\bphotocop(?:y|ies|iable)\b|'
             r'\bprint[\s-]?(?:friendly|version)\b|\bno\s+colou?rs?\b', {'preset': 'mono', 'print': True}),
    ('high-legibility', r'\bdyslexi\w*\b|\bhigh[\s-]?legibility\b|\blarge[\s-]?print\b|\beasy[\s-]to[\s-]read\b|'
                        r'\bvisually\s+impaired\b', {'preset': 'high-legibility'}),
    ('bold-dark', r'\bbold[\s-]dark\b|\bcinematic\b|\bdramatic\b|\bpitch\s+deck\b', {'preset': 'bold-dark'}),
    ('editorial', r'\beditorial\b|\belegant\b|\bmagazine\b|\bliterary\b|\bclassic(?:al)?\s+(?:look|style|design)\b|'
                  r'\bserif\b', {'preset': 'editorial'}),
    ('minimal', r'\bminimal(?:ist|istic)?\b|\bclean\s+(?:look|design|style)\b|\bsleek\b|\bsimple\s+design\b',
     {'preset': 'minimal'}),
    ('vibrant', r'\bvibrant\b|\bcolou?rful\b|\bplayful\b|\bfun\b|\bbright\b|\bfor\s+kids\b|\bcreative\b|\bbold\b',
     {'preset': 'vibrant'}),
    ('pastel', r'\bpastels?\b|\bsoft\s+colou?rs?\b|\bcalm(?:ing)?\b|\bgentle\b', {'preset': 'pastel'}),
    ('academic', r'\bacademic\b|\blab\s+report\b|\bresearch\s+poster\b|\bscientific\s+(?:paper|poster|report)\b|'
                 r'\bthesis\b|\bformal\b', {'preset': 'academic'}),
    ('dark', r'\bdark\s+(?:design|theme|mode|style|background|slides?|colou?rs?|look)\b|\bin\s+dark\b|\bdark\b',
     {'dark': True}),
    ('light', r'\blight\s+(?:design|theme|mode|style|background|slides?|colou?rs?|look)\b|\bwhite\s+background\b',
     {'dark': False}),
]
# "bold" and "creative" with a dark word become bold-dark, not vibrant-on-dark (run 2808)
DARK_MOOD = {'vibrant': 'bold-dark'}


@dataclass
class PresetInfo:
    id: str                  # PRESETS
    name: str                # 'Bold dark'
    description: str         # who it is for (the table in 3.2)
    dark: bool
    families: dict           # role -> family
    colors: dict             # bg, text, accent, accent2 (RRGGBB) for the picker swatch
    thumb: str               # '/api/design/presets/<id>/thumb'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TemplateInfo:
    id: str                  # TEMPLATES
    name: str
    description: str
    format: str              # 'pptx' | 'pdf' | 'docx'
    preset: str              # PRESETS
    paper: str | None        # 'a4' | 'letter' | 'a3' | 'a2' | None (slides)
    sequence: list[str]      # layout ids the page sequence starts with, e.g. ['cover-hero', 'title-bullets', ...]
    tone: list[str]          # tone rules given to the content writer ("short bullets", "cite sources")

    def to_dict(self) -> dict:
        return asdict(self)


TEMPLATES = ('class-presentation', 'lab-report', 'research-poster', 'revision-notes', 'infographic', 'book-report',
             'science-fair')


@dataclass
class PromptStyle:
    preset: str | None = None            # PRESETS id the words chose
    dark: bool | None = None             # dark/light override
    mood: list[str] = None               # matched PROMPT_WORDS ids in text order
    fonts: dict | None = None            # role -> requested family
    print_version: bool = False
    words: list[str] = None              # the matched spans, for the design report

    def __post_init__(self):
        self.mood = self.mood or []
        self.words = self.words or []


# ---------- the presets (F): each a complete DesignSystem, tuned for students ----------

# Okabe-Ito, the colour-blind-safe set; each preset orders it for its accent and repair() keeps every colour at 3:1
# on the background. `overlay` is a tint of the background: text on a photo is drawn in `text` over it, and repair()
# raises overlay_alpha until that reads against the worst photo pixel.
OKABE_LIGHT = ['0072B2', 'D55E00', '009E73', 'CC79A7', 'E69F00', '56B4E9']
OKABE_DARK = ['56B4E9', 'E69F00', '009E73', 'F0E442', 'CC79A7', 'D55E00']


def _colors(bg, surface, text, heading, muted, accent, accent2, border, header_bg, header_text, stripe, code_bg,
            overlay, on_accent) -> dict:
    return {'bg': bg, 'surface': surface, 'text': text, 'heading': heading, 'muted': muted, 'accent': accent,
            'accent2': accent2, 'border': border, 'header_bg': header_bg, 'header_text': header_text, 'stripe': stripe,
            'code_bg': code_bg, 'overlay': overlay, 'on_accent': on_accent}


# id -> (name, description, DesignSystem keyword arguments). tokens.repair() runs over each at get().
PRESET_DATA: dict[str, tuple[str, str, dict]] = {
    'bold-dark': ('Bold dark', 'Pitches, space and tech topics, strong covers: dark backgrounds, big type, one hot '
                               'accent.', dict(
        dark=True,
        colors=_colors('0B1020', '161D33', 'F4F6FB', 'FFFFFF', 'A9B2C7', 'FF7A45', '4CC9F0', '2A3350', '1E2A4A',
                       'FFFFFF', '121932', '161D33', '05070F', '111111'),
        chart_palette=list(OKABE_DARK), gradients=[('0B1020', '3A1C71'), ('FF7A45', 'F72585')], overlay_alpha=0.6,
        families=dict(display='Space Grotesk', heading='Space Grotesk', body='Inter', caption='Inter',
                      mono='JetBrains Mono'),
        scale=dict(ratio=1.414, line_height=1.25, display_line_height=1.0, display_tracking=-0.02),
        shape=dict(radius=12, stroke=1.0, shadow=False, accent_shape='bar'), image='full-bleed')),
    'editorial': ('Editorial', 'History, literature and essays: serif display type and generous white space.', dict(
        dark=False,
        colors=_colors('FBF8F3', 'FFFFFF', '1F1B16', '111111', '5E554A', '9F1239', '1E3A8A', 'E3DCD0', '2B2118',
                       'FFFFFF', 'F4EFE6', 'F1ECE3', 'FBF8F3', 'FFFFFF'),
        chart_palette=['9F1239', '0072B2', '009E73', 'D55E00', 'CC79A7', '56B4E9'],
        gradients=[('FBF8F3', 'F1E4D3'), ('9F1239', '1E3A8A')],
        families=dict(display='Playfair Display', heading='Playfair Display', body='Source Serif 4',
                      caption='Source Sans 3', mono='JetBrains Mono'),
        scale=dict(ratio=1.333, line_height=1.45, display_line_height=1.1, display_tracking=-0.01),
        spacing=dict(page_margin=64.0, slide_margin=56.0),
        shape=dict(radius=0, stroke=0.75, shadow=False, accent_shape='underline'), image='framed')),
    'minimal': ('Minimal', 'Science and maths: lots of air and one accent colour.', dict(
        dark=False,
        colors=_colors('FFFFFF', 'F6F8FA', '1F2328', '0F172A', '57606A', '2563EB', '0EA5E9', 'D0D7DE', '0F172A',
                       'FFFFFF', 'F3F6FA', 'F4F4F5', 'FFFFFF', 'FFFFFF'),
        chart_palette=list(OKABE_LIGHT), gradients=[('FFFFFF', 'EEF2F7'), ('2563EB', '0EA5E9')],
        families=dict(display='Inter', heading='Inter', body='Inter', caption='Inter', mono='JetBrains Mono'),
        scale=dict(ratio=1.25, line_height=1.3, display_line_height=1.05, display_tracking=-0.01),
        shape=dict(radius=6, stroke=1.0, shadow=False, accent_shape='bar'), image='full-bleed')),
    'vibrant': ('Vibrant', 'School projects and younger students: playful shapes and a bright palette.', dict(
        dark=False,
        colors=_colors('FFFDF7', 'FFFFFF', '1A1A2E', '3A0CA3', '55536B', 'C2185B', '4361EE', 'EDE6D6', '3A0CA3',
                       'FFFFFF', 'FFF4E0', 'FFF1E6', 'FFFDF7', 'FFFFFF'),
        chart_palette=['D55E00', '0072B2', '009E73', 'CC79A7', 'E69F00', '56B4E9'],
        gradients=[('FFB703', 'F72585'), ('4361EE', '4CC9F0')],
        families=dict(display='Fredoka', heading='Poppins', body='Nunito', caption='Nunito', mono='JetBrains Mono'),
        scale=dict(ratio=1.333, line_height=1.3, display_line_height=1.05, display_tracking=0.0),
        shape=dict(radius=20, stroke=2.0, shadow=True, accent_shape='blob'), image='rounded')),
    'pastel': ('Pastel', 'Biology, wellbeing and revision notes: soft colours and rounded shapes.', dict(
        dark=False,
        colors=_colors('FBF7FF', 'FFFFFF', '2E2A3A', '4C3F7A', '5F5A6E', '6D5BD0', 'E8A0BF', 'E4DDF0', 'EDE7FB',
                       '2E2A3A', 'F6F1FD', 'F3EEFA', 'FBF7FF', 'FFFFFF'),
        chart_palette=['6D5BD0', '009E73', 'CC79A7', '0072B2', 'E69F00', 'D55E00'],
        gradients=[('FBF7FF', 'FDE2F3'), ('C9B8FF', 'FFD6E8')],
        families=dict(display='Quicksand', heading='Quicksand', body='Nunito', caption='Nunito', mono='JetBrains Mono'),
        scale=dict(ratio=1.25, line_height=1.4, display_line_height=1.1, display_tracking=0.0),
        shape=dict(radius=16, stroke=1.0, shadow=False, accent_shape='circle'), image='rounded')),
    'academic': ('Academic', 'Lab reports and research posters: print first, with a strict hierarchy.', dict(
        dark=False,
        colors=_colors('FFFFFF', 'F8FAFC', '111827', '1E3A5F', '4B5563', '1D4ED8', 'B91C1C', 'CBD5E1', '1E3A5F',
                       'FFFFFF', 'F1F5F9', 'F3F4F6', 'FFFFFF', 'FFFFFF'),
        chart_palette=list(OKABE_LIGHT), gradients=[('FFFFFF', 'F1F5F9'), ('1E3A5F', '1D4ED8')],
        families=dict(display='Source Serif 4', heading='Source Sans 3', body='Source Serif 4', caption='Source Sans 3',
                      mono='Source Code Pro'),
        scale=dict(ratio=1.25, line_height=1.35, display_line_height=1.15, display_tracking=0.0),
        shape=dict(radius=2, stroke=0.75, shadow=False, accent_shape='none'), image='framed', justify=True)),
    'mono': ('Mono', 'Print and photocopy friendly: black on white, greys only, charts told apart by pattern.', dict(
        dark=False,
        colors=_colors('FFFFFF', 'FFFFFF', '000000', '000000', '4D4D4D', '1A1A1A', '595959', '8C8C8C', '000000',
                       'FFFFFF', 'F2F2F2', 'F2F2F2', 'FFFFFF', 'FFFFFF'),
        chart_palette=['1A1A1A', '595959', '8C8C8C', '404040', '737373', 'A6A6A6'],
        gradients=[('FFFFFF', 'E6E6E6')],
        families=dict(display='IBM Plex Sans', heading='IBM Plex Sans', body='IBM Plex Sans', caption='IBM Plex Sans',
                      mono='IBM Plex Mono'),
        scale=dict(ratio=1.25, line_height=1.3, display_line_height=1.05, display_tracking=-0.01),
        shape=dict(radius=0, stroke=1.0, shadow=False, accent_shape='bar'), image='framed', hatch=True,
        grey_images=True)),
    'high-legibility': ('High legibility', 'Dyslexia-friendly: Atkinson Hyperlegible and Lexend, larger sizes and '
                                           'spacing, never justified.', dict(
        dark=False,
        colors=_colors('FFFDF7', 'FFFFFF', '1A1A1A', '102A43', '4A4A4A', '0B57D0', 'B3261E', 'A3A3A3', '102A43',
                       'FFFFFF', 'F4F1E8', 'F1EEE6', 'FFFDF7', 'FFFFFF'),
        chart_palette=list(OKABE_LIGHT), gradients=[('FFFDF7', 'F4F1E8')],
        families=dict(display='Lexend', heading='Lexend', body='Atkinson Hyperlegible', caption='Atkinson Hyperlegible',
                      mono='JetBrains Mono'),
        scale=dict(ratio=1.25, line_height=1.5, display_line_height=1.15, display_tracking=0.0),
        spacing=dict(gutter=24.0, slide_margin=56.0),
        shape=dict(radius=8, stroke=1.5, shadow=False, accent_shape='underline'), image='rounded',
        min_sizes=dict(slide_body=22.0, slide_caption=16.0, print_body=12.0, print_caption=10.0), justify=False)),
}
assert tuple(PRESET_DATA) == PRESETS

TEMPLATE_DATA: dict[str, TemplateInfo] = {t.id: t for t in (
    TemplateInfo('class-presentation', 'Class presentation', 'A talk for your class: a strong cover, one idea per '
                 'slide, pictures and a diagram.', 'pptx', 'minimal', None,
                 ['cover-hero', 'title-bullets', 'image-left-text', 'big-number', 'full-width-diagram', 'two-column',
                  'quote', 'closing'],
                 ['one idea per slide', 'at most 5 short bullets', 'speaker notes carry the detail',
                  'end with a summary slide']),
    TemplateInfo('lab-report', 'Lab report', 'Aim, method, results and conclusion, with figures and references.',
                 'pdf', 'academic', 'a4',
                 ['cover', 'chapter-opener', 'text-side-figure', 'full-figure', 'key-points', 'references'],
                 ['formal and objective', 'past tense for the method', 'label every figure and table',
                  'cite sources']),
    TemplateInfo('research-poster', 'Research poster', 'One large page: question, method, findings and a clear '
                 'takeaway.', 'pdf', 'academic', 'a2',
                 ['cover', 'two-column-text', 'full-figure', 'key-points', 'references'],
                 ['short sections', 'one key figure', 'findings in plain sentences', 'cite sources']),
    TemplateInfo('revision-notes', 'Revision notes', 'Key points, definitions and summaries to learn from.', 'pdf',
                 'pastel', 'a4', ['chapter-opener', 'key-points', 'two-column-text', 'key-points'],
                 ['short bullet points', 'bold the key terms', 'a summary box per topic', 'examples over theory']),
    TemplateInfo('infographic', 'Infographic', 'Numbers and a story on one page: big stats, icons and a diagram.',
                 'pdf', 'vibrant', 'a3', ['cover', 'full-figure', 'key-points'],
                 ['few words', 'lead with numbers', 'one idea per panel', 'give the source of every number']),
    TemplateInfo('book-report', 'Book report', 'The book, its themes and characters, quotes and your opinion.', 'pdf',
                 'editorial', 'a4', ['cover', 'chapter-opener', 'pull-quote', 'text-side-figure', 'references'],
                 ['your own view, backed by quotes', 'no spoilers in the summary unless asked',
                  'name the author and edition']),
    TemplateInfo('science-fair', 'Science fair board', 'Question, hypothesis, experiment, results and conclusion on '
                 'one board.', 'pdf', 'vibrant', 'a2',
                 ['cover', 'two-column-text', 'full-figure', 'key-points', 'references'],
                 ['a clear question', 'a testable hypothesis', 'results as a chart', 'what you would do next']),
)}
assert tuple(TEMPLATE_DATA) == TEMPLATES

# prompt words that look like mood or dark words but name a topic ("the Dark Ages", "fun facts", "sans serif")
NOT_STYLE = re.compile(
    r'\bdark\s+(?:ages?|matter|energy|side|web|net|chocolate|reactions?|horse|magic|arts?|souls?|knight|room|times|'
    r'age|forest|hole|star|skin(?:ned)?|haired|green|red|blue|brown|patterns?|adaptation|current|field|mode\s+of)\b|'
    r'\bfun\s+facts?\b|\bcreative\s+(?:writing|commons|industr\w*)\b|\bbold\s+(?:text|font|type|words?|headings?|'
    r'titles?|terms?|keywords?|face)\b|\bin\s+bold\b|\bsans[\s-]+serif\b|\bformal\s+(?:letter|logic|language|'
    r'languages|methods?|verification|proofs?)\b', re.I)
# an exact preset id or name ("bold-dark", "high legibility", "the vibrant preset")
# One-word ids are also ordinary words ("a minimal design"), so they count as a name only with preset/style/theme/look
# after them; otherwise they are mood words (rule 3) and accessibility words still win over them.
PRESET_NAME = [(pid, re.compile(r'\b' + r'[\s-]'.join(map(re.escape, pid.split('-'))) +
                                (r'\b(?:\s+(?:preset|style|theme|look|template))?' if '-' in pid else
                                 r'\s+(?:preset|style|theme|look|template)\b'), re.I)) for pid in PRESETS]
# font roles a request can name ("headings in Poppins", "Lexend for the titles", "body text in Lora")
ROLE_WORDS = {'heading': r'headings?|headers?|titles?|headlines?', 'display': r'display|cover\s+titles?',
              'body': r'body(?:\s+text)?|paragraphs?|body\s+copy'}
FONT_NAME = r'([A-Z][\w-]*(?:\s+(?:[A-Z0-9][\w-]*|[0-9]+)){0,3})'


def _system(pid: str) -> DesignSystem:
    from .tokens import Families, MinSizes, Shape, Spacing, TypeScale, repair
    name, _desc, kw = PRESET_DATA[pid]
    kw = copy.deepcopy(kw)
    ds = DesignSystem(
        id=pid, name=name, dark=kw.pop('dark'), colors=kw.pop('colors'), chart_palette=kw.pop('chart_palette'),
        gradients=[tuple(g) for g in kw.pop('gradients', [])], overlay_alpha=kw.pop('overlay_alpha', 0.55),
        families=Families(**kw.pop('families', {})), scale=TypeScale(**kw.pop('scale', {})),
        spacing=Spacing(**kw.pop('spacing', {})), shape=Shape(**kw.pop('shape', {})),
        min_sizes=MinSizes(**kw.pop('min_sizes', {})), source=[f'preset:{pid}'], **kw)
    return repair(ds, quiet=True)


def get(preset_id: str) -> DesignSystem:
    """The complete DesignSystem of a preset; an unknown id gives DEFAULT_PRESET's."""
    pid = preset_id if preset_id in PRESETS else LEGACY_THEME.get(str(preset_id or ''), DEFAULT_PRESET)
    return _system(pid)


def list_presets() -> list[PresetInfo]:
    """Every preset in PRESETS order, for GET /api/design/presets."""
    out = []
    for pid in PRESETS:
        ds = get(pid)
        out.append(PresetInfo(id=pid, name=ds.name, description=PRESET_DATA[pid][1], dark=ds.dark,
                              families=asdict(ds.families),
                              colors={k: ds.colors[k] for k in ('bg', 'text', 'accent', 'accent2')},
                              thumb=f'/api/design/presets/{pid}/thumb'))
    return out


def list_templates() -> list[TemplateInfo]:
    """Every student template in TEMPLATES order."""
    return [copy.deepcopy(TEMPLATE_DATA[t]) for t in TEMPLATES]


def template(template_id: str) -> TemplateInfo | None:
    """One template by id, or None."""
    t = TEMPLATE_DATA.get(str(template_id or ''))
    return copy.deepcopy(t) if t else None


def _spans(rx: str, text: str) -> list[re.Match]:
    return [m for m in re.finditer(rx, text, re.I) if not _masked(m, text)]


def _masked(m: re.Match, text: str) -> bool:
    """True when a match sits inside a topic phrase (NOT_STYLE), not a style request."""
    return any(n.start() <= m.start() < n.end() for n in NOT_STYLE.finditer(text))


def _known_family(name: str, text: str, end: int) -> bool:
    """A captured name is a font when it is a known family (open catalogue, similarity table, Office) or the request
    says "font" right after it; "titles in French" is not a font request."""
    from .fonts import CATALOG, OFFICE_FONTS, SIMILAR, key
    k = key(name)
    known = {key(f) for f in (*CATALOG, *OFFICE_FONTS, *SIMILAR)}
    return k in known or bool(re.match(r'\s*(?:font|typeface)\b', text[end:], re.I))


def _fonts_of(text: str) -> dict | None:
    """Families a request names, by role: "headings in Poppins" -> heading, else the brief's font -> body."""
    from ..create.brief import font_of
    from .fonts import display
    out: dict[str, str] = {}
    for role, words in ROLE_WORDS.items():
        for rx in (rf'\b(?:{words})\s+(?:in|using|with|set\s+in)\s+{FONT_NAME}',
                   rf'\b(?:use\s+)?{FONT_NAME}\s+(?:font\s+)?for\s+(?:the\s+|all\s+)?(?:{words})\b'):
            found = None
            for m in re.finditer(rx, text):
                words_ = m.group(1).split()
                # "Poppins Bold": trailing style words are not part of the family
                while words_ and words_[-1].lower() in ('bold', 'italic', 'regular', 'light', 'font', 'please'):
                    words_.pop()
                for n in range(len(words_), 0, -1):   # the longest known family among the leading words
                    cand = ' '.join(words_[:n])
                    if _known_family(cand, text, m.start(1) + len(cand)):
                        found = cand
                        break
                if found:
                    break
            if found:
                out.setdefault(role, display(found))
                break
    named = font_of(text)
    if named and 'body' not in out:
        fam = display(named)
        if fam not in (out.get('heading'), out.get('display')):
            out['body'] = fam
    if out.get('heading') and 'display' not in out:
        out['display'] = out['heading']
    return out or None


def read_prompt(text: str) -> PromptStyle:
    """The style a request's words ask for, by PROMPT_WORDS and the precedence above. No LLM. Empty text -> PromptStyle()."""
    t = ' '.join(str(text or '').split())[:4000]
    if not t:
        return PromptStyle()
    hits: list[tuple[int, str, dict, str]] = []   # (position, id, effect, span)
    for pid, rx, effect in PROMPT_WORDS:
        for m in _spans(rx, t):
            hits.append((m.start(), pid, effect, m.group(0)))
    hits.sort(key=lambda h: h[0])
    exact = sorted(((m.start(), pid, m.group(0)) for pid, prx in PRESET_NAME for m in prx.finditer(t)
                    if not _masked(m, t)), key=lambda x: x[0])
    # "mono" alone is an exact name only when it isn't about a font ("a mono font", "monospace")
    exact = [e for e in exact if not (e[1] == 'mono' and re.match(r'mono\s*(?:font|spaced?|type)', t[e[0]:], re.I))]
    preset = None
    print_version = any(h[2].get('print') for h in hits)
    if exact:
        preset = exact[0][1]
    else:
        access = [h for h in hits if h[1] in ('mono', 'high-legibility')]
        mood = [h for h in hits if 'preset' in h[2] and h[1] not in ('mono', 'high-legibility')]
        if access:
            preset = access[0][2]['preset']
        elif mood:
            preset = mood[0][2]['preset']
    dark = None
    dl = [h for h in hits if 'dark' in h[2]]
    if dl:
        dark = dl[0][2]['dark']
    if dark and preset in DARK_MOOD and not (exact and exact[0][1] == preset):
        preset = DARK_MOOD[preset]
    mood_ids = list(dict.fromkeys(h[1] for h in hits))
    words = list(dict.fromkeys([*(e[2] for e in exact), *(h[3] for h in hits)]))
    try:
        fonts = _fonts_of(t)
    except Exception:
        fonts = None
    return PromptStyle(preset=preset, dark=dark, mood=mood_ids, fonts=fonts, print_version=print_version, words=words)


def for_theme(theme: str | None) -> str:
    """The preset for a legacy theme name (LEGACY_THEME), DEFAULT_PRESET for None or unknown."""
    return LEGACY_THEME.get(theme or '', DEFAULT_PRESET)
