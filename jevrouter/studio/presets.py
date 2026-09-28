"""The 8 presets, the student templates and prompt-word mapping (docs/PLAN-designer.md 3.2, 4 and 9.3).

Owner: builder F. PRESETS, TEMPLATES, LEGACY_THEME and PROMPT_WORDS are contract data; the functions are stubs until F
builds them.
"""
from __future__ import annotations

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


def get(preset_id: str) -> DesignSystem:
    """The complete DesignSystem of a preset; an unknown id gives DEFAULT_PRESET's."""
    raise NotImplementedError('studio.presets.get: builder F')


def list_presets() -> list[PresetInfo]:
    """Every preset in PRESETS order, for GET /api/design/presets."""
    raise NotImplementedError('studio.presets.list_presets: builder F')


def list_templates() -> list[TemplateInfo]:
    """Every student template in TEMPLATES order."""
    raise NotImplementedError('studio.presets.list_templates: builder F')


def template(template_id: str) -> TemplateInfo | None:
    """One template by id, or None."""
    raise NotImplementedError('studio.presets.template: builder F')


def read_prompt(text: str) -> PromptStyle:
    """The style a request's words ask for, by PROMPT_WORDS and the precedence above. No LLM. Empty text -> PromptStyle()."""
    raise NotImplementedError('studio.presets.read_prompt: builder F')


def for_theme(theme: str | None) -> str:
    """The preset for a legacy theme name (LEGACY_THEME), DEFAULT_PRESET for None or unknown."""
    return LEGACY_THEME.get(theme or '', DEFAULT_PRESET)
