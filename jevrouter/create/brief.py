"""What a request asks of a created file (docs/PLAN-accuracy-v2.md C1): its format, page or slide count, theme, font,
images and diagrams, read from the user's original query and the step text with no LLM.

The pipeline builds the primary file step's brief as merge(parse_brief(query), parse_brief(step_text)), so constraints
the planner dropped from the step text ("12-13 pages", "black and white") still reach the file.
"""
import re
from dataclasses import asdict, dataclass, field


@dataclass
class Brief:
    format: str | None = None              # detect_format over the original query, then the step text
    pages: tuple[int, int] | None = None   # "12-13 page", "twelve pages", "10 page report", "at least 5 pages"
    slides: tuple[int, int] | None = None  # "20-slide deck", "10 slides"
    theme: str | None = None               # 'mono' for black and white|black & white|b&w|b/w|monochrome|grayscale|greyscale|black-and-white; else dark|warm per THEME
    font: str | None = None                # the family the user named, normalised ('anthropic sans', 'inter')
    images: bool = False                   # images|photos|pictures|figures|illustrations
    image_source: str | None = None        # 'web' when "from the web|online|sources"
    diagrams: bool = False                 # diagram(s)|flowchart|timeline|tree|hierarchy|chart(s) (charts also stay charts)
    diagram_kinds: list[str] = field(default_factory=list)   # subset of WRITABLE_KINDS
    words: int | None = None               # "2000 words"
    capped: bool = False                   # pages or slides above MAX_PAGES/MAX_SLIDES were capped
    design: str | None = None              # the design file's name, set by agents/create.make (FileBrief.design);
                                           # describe() never mentions it: the model never sees styling


MAX_PAGES = 40
MAX_SLIDES = 40
WORDS_PER_PAGE = 420   # A4, 10.5 pt, 2 cm margins, mixed prose/tables; measured start value (2741 step-1 spec: 1,889 words -> 5 pages ~ 380/page with 4 tables), recalibrated by C3's loop
# longer names first where one contains another ("helvetica neue" before "helvetica")
KNOWN_FONTS = ('anthropic sans', 'anthropic serif', 'inter', 'roboto', 'open sans', 'lato', 'source sans', 'noto sans',
               'helvetica neue', 'helvetica', 'arial', 'times new roman', 'georgia', 'garamond', 'calibri',
               'ibm plex sans', 'dejavu sans', 'poppins', 'lexend', 'atkinson hyperlegible', 'playfair display',
               'merriweather', 'jetbrains mono', 'space grotesk', 'source serif', 'dm sans', 'work sans', 'sf pro',
               'segoe ui')
DIAGRAM_KINDS = ('timeline', 'tree', 'flow')

NUMBER_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven',
                'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty']
TENS = {'twenty': 20, 'thirty': 30, 'forty': 40}
_UNITS = '|'.join(sorted(NUMBER_WORDS[1:10], key=len, reverse=True))
_WORD_NUM = (r'(?:(?:twenty|thirty)[\s-](?:' + _UNITS + r')|forty|thirty|' +
             '|'.join(sorted(NUMBER_WORDS[1:], key=len, reverse=True)) + ')')
_NUM = rf'(?:\d{{1,3}}|{_WORD_NUM})'
_RANGE = rf'(?P<lo>{_NUM})(?:\s*(?:-|\u2013|\u2014|to|or)\s*(?P<hi>{_NUM}))?'
PAGES = re.compile(rf'\b(?:at\s+least\s+|about\s+|around\s+|roughly\s+|some\s+)?{_RANGE}[\s-]*pages?\b', re.I)
SLIDES = re.compile(rf'\b{_RANGE}[\s-]*(?:slides?|slide\s+(?:deck|presentation))\b', re.I)
WORDS = re.compile(r'\b(\d{1,2},\d{3}|\d{3,5})[\s-]*words?\b', re.I)

# Themes. `create_agent.THEME` uses these too, so the brief and the create agent read a request the same way.
MONO = re.compile(r'\bblack[\s-]*(?:and|&|n)?[\s-]*white\b|\bb\s*&\s*w\b|\bb/w\b|\bmono(?:chrome|chromatic)?\b(?!\s*font)|'
                  r'\bgr[ae]y[\s-]?scale\b|\bno\s+colou?rs?\b', re.I)
# "a dark design", "dark slides", "dark-themed", "in dark" (run 2808 asked for "a dark design" and got white slides)
DARK = re.compile(r'\bdark\s+(?:design|theme|mode|style|look|feel|'
                  r'aesthetic|palette|scheme|background|backgrounds|slides?|deck|presentation|version|colou?rs?|'
                  r'tones?|ui)\b|\bdark[\s-]+(?:themed|styled|coloured|colored)\b|\bin\s+dark\b', re.I)
WARM = re.compile(r'\bwarm\s+(?:theme|style|colou?rs?|tones?)\b', re.I)
THEMES = {'mono': MONO, 'dark': DARK, 'warm': WARM}

IMAGES = re.compile(r'\b(?:images?|photos?|photographs?|pictur(?:e|es|ed)|pictorial|illustrat(?:ion|ions|ed)|figures?|'
                    r'visuals?)\b', re.I)
WEB = re.compile(r'\b(?:from\s+the\s+(?:web|internet)|online|web|internet|wikimedia|commons|sourced|\w*sources?)\b', re.I)
DIAGRAMS = re.compile(r'\b(?:diagrams?|flow\s?charts?|timelines?|hierarch(?:y|ies|ical)|tree\s+(?:diagram|chart|'
                      r'structure|view)s?|charts?|graphs?|mind\s?maps?|org(?:anisation|anization)?\s+charts?)\b', re.I)
KIND_WORDS = {
    'timeline': re.compile(r'\btimelines?\b|\bchronolog', re.I),
    'tree': re.compile(r'\btree\s+(?:diagram|chart|structure|view)s?\b|\bhierarch|\btaxonom|\bmind\s?maps?\b|'
                       r'\borg(?:anisation|anization)?\s+charts?\b', re.I),
    'flow': re.compile(r'\bflow\s?charts?\b|\bflow\s+diagrams?\b|\bprocess\s+diagrams?\b|\bworkflow\s+diagrams?\b', re.I),
}
# The Studio diagram kinds (docs/PLAN-designer.md 3.4) named in a request, read by `studio_kinds`. The writer's schema
# (a generic diagram block) and the long-document prompts offer them, so `diagram_kinds` holds them too, except
# `labelled`: its picture comes only from code, so a model can't write one. WRITES_AS is the legacy kind each one can
# be written as (kept for callers that only know timeline, tree and flow).
STUDIO_DIAGRAM_KINDS = ('cycle', 'venn', 'pyramid', 'matrix', 'mindmap', 'process', 'comparison', 'labelled',
                        'stat-cards', 'scatter')
STUDIO_KIND_WORDS = {
    'cycle': re.compile(r'\b(?:life|water|carbon|nitrogen|rock|business|product|cell)\s+cycles?\b|\bcycle\s+diagrams?\b|'
                        r'\bcyclical\b|\bcircular\s+(?:diagram|flow|process)s?\b', re.I),
    'venn': re.compile(r'\bvenn(?:\s+diagrams?)?\b|\boverlapping\s+circles\b', re.I),
    'pyramid': re.compile(r'\bpyramid\s+(?:diagram|chart|graphic)s?\b|\b(?:food|energy|population|needs)\s+pyramids?\b|'
                          r'\bhierarchy\s+of\s+needs\b', re.I),
    'matrix': re.compile(r'\b(?:2|two)\s*(?:x|by|×)\s*(?:2|two)\b|\bquadrant\s+(?:chart|diagram|matrix)s?\b|'
                         r'\bswot(?:\s+analysis)?\b|\bpriority\s+matrix\b', re.I),
    'mindmap': re.compile(r'\bmind\s?maps?\b|\bconcept\s+maps?\b|\bspider\s+diagrams?\b', re.I),
    'process': re.compile(r'\bprocess\s+(?:arrows?|steps?|diagrams?)\b|\bstep[\s-]by[\s-]step\s+(?:diagram|graphic)s?\b|'
                          r'\bchevrons?\b', re.I),
    'comparison': re.compile(r'\bcomparison\s+(?:cards?|table|diagram|chart)s?\b|\bpros\s+(?:and|&)\s+cons\b|'
                             r'\bside[\s-]by[\s-]side\b', re.I),
    'labelled': re.compile(r'\blabell?ed\s+(?:diagram|figure|picture|image|photo|drawing)s?\b|'
                           r'\bannotated\s+(?:diagram|figure|picture|image|photo)s?\b', re.I),
    'stat-cards': re.compile(r'\b(?:stat(?:istic)?s?|kpi|number|figure)\s+cards?\b|\bkey\s+(?:stats|statistics|numbers|'
                             r'figures)\b|\bbig\s+numbers?\b', re.I),
    'scatter': re.compile(r'\bscatter\s*(?:plots?|graphs?|charts?|diagrams?)?\b|\bcorrelation\s+(?:plot|graph)s?\b',
                          re.I),
}
WRITES_AS = {'cycle': 'flow', 'process': 'flow', 'mindmap': 'tree', 'pyramid': 'tree'}
WRITABLE_KINDS = DIAGRAM_KINDS + tuple(k for k in STUDIO_DIAGRAM_KINDS if k != 'labelled')


def studio_kinds(text: str) -> list[str]:
    """The Studio diagram kinds a request names ("a venn diagram", "a SWOT", "a labelled diagram of a cell"), in
    STUDIO_DIAGRAM_KINDS order; no LLM."""
    t = ' '.join(str(text or '').split())
    return [k for k in STUDIO_DIAGRAM_KINDS if STUDIO_KIND_WORDS[k].search(t)]
# Images and diagrams count only when asked for as content of the file ("with images", "add 3 diagrams", "a flowchart
# of the login", "images from the web"), not when they are the topic ("a pdf about graph theory", "put these sales
# figures in a pdf", "a summary of the image processing answer").
ASK_WORDS = {'with', 'include', 'includes', 'included', 'including', 'add', 'adding', 'added', 'contain', 'contains',
             'containing', 'and', 'plus', 'has', 'have', 'having', 'put', 'insert', 'embed', 'use', 'using', 'also',
             'some', 'few', 'multiple', 'several', 'many', 'various', 'relevant', 'a', 'an', 'show', 'draw', 'make',
             'create', 'generate', 'build', 'lots', 'more', 'two', 'three', 'four', 'five', 'six', 'illustrated', 'needs'}
SUBJECT_WORDS = {'about', 'on', 'regarding', 'concerning', 'of', 'explaining', 'covering', 'the', 'these', 'those', 'my',
                 'your', 'that', 'this', 'our'}
NOUN_ADJUNCT = re.compile(r'\s+(?:theory|theorems?|processing|recognition|classification|compression|formats?|files?|'
                          r'editing|generation|quality|sizes?|data(?:base)?s?|neural|api|library|librar(?:y|ies)|'
                          r'algorithms?|of\s+speech|captioning|segmentation|analysis|models?)\b', re.I)
NUMBER_FIGURES = re.compile(r'\b(?:sales|revenue|key|financial|population|trade|budget|profit|export|import|gdp|latest|'
                            r'official|monthly|annual|quarterly|economic|headline|census|these|those|the)\s+figures\b',
                            re.I)
FROM_WEB = re.compile(r'\s+(?:from\s+(?:the\s+)?(?:web|internet|wikimedia|commons|online|sources?)|online)\b', re.I)


def asked_for(rx: re.Pattern, text: str) -> bool:
    """True when a noun `rx` matches is asked for as content of the file, not named as its topic."""
    for m in rx.finditer(text):
        after = text[m.end():]
        if NOUN_ADJUNCT.match(after) or NUMBER_FIGURES.search(text[max(0, m.start() - 40):m.end()]) and \
                m.group(0).lower().startswith('figure'):
            continue
        if FROM_WEB.match(after):
            return True
        before = re.findall(r"[a-z0-9][\w'-]*", text[:m.start()].lower())[-3:]
        for w in reversed(before):
            if w in ASK_WORDS or w.isdigit():
                return True
            if w in SUBJECT_WORDS and w != 'the':
                break
    return False


# "multiple diagrams", "several diagrams", "2 diagrams": the file needs at least two, so the kinds are filled to two
MANY_DIAGRAMS = re.compile(r'\b(?:multiple|several|many|various|some|a\s+few|few|lots\s+of|two|three|four|five|[2-9])'
                           r'\s+(?:\w+\s+)?diagrams\b|\bdiagrams\b', re.I)

FONT_FILLER = {'also', 'use', 'using', 'with', 'in', 'text', 'the', 'a', 'an', 'and'}
# Words that describe a font rather than name one ("a bigger font", "a serif font").
FONT_DESCRIBERS = {'bold', 'big', 'bigger', 'large', 'larger', 'small', 'smaller', 'same', 'different', 'nice', 'good',
                   'readable', 'clean', 'simple', 'proper', 'default', 'this', 'that', 'my', 'your', 'any', 'some', 'one',
                   'other', 'serif', 'sans', 'sans-serif', 'monospace', 'monospaced', 'mono', 'modern', 'classic',
                   'professional', 'standard', 'normal', 'regular', 'formal', 'fancy', 'nicer', 'better', 'custom',
                   'black', 'white', 'dark', 'light', 'body', 'heading', 'headings', 'title', 'main', 'new', 'old',
                   'cool', 'pretty', 'beautiful', 'elegant', 'properly', 'sized', 'size', 'font', 'fonts', 'of', 'for',
                   'to', 'as', 'is', 'be', 'it', 'please', 'only', 'all', 'same', 'whole', 'entire', 'document', 'no',
                   'not', 'without', 'more', 'less', 'which', 'what', 'good-looking', 'larger', 'readable'}
# a singular "font" asked for as a style ("Acme Grotesk font"); "fonts" and "font rendering" are topics
FONT_BEFORE = re.compile(r'\bfont\b(?!\s*(?:size|sizes|sizing|colou?r|weight|rendering|design|designers?|famil(?:y|ies)|'
                         r'files?|formats?|licens\w*|history|pairing|choice|choices|stack)\b)', re.I)
# words the family name can't run past: formats, verbs and topic words ("slides explaining how font ...")
FONT_STOP = {'pdf', 'pdfs', 'slides', 'slide', 'deck', 'docx', 'doc', 'document', 'report', 'presentation', 'file',
             'about', 'on', 'of', 'how', 'why', 'what', 'history', 'explaining', 'comparing', 'describing', 'is', 'are',
             'was', 'make', 'write', 'create', 'build', 'set', 'use', 'using', 'with', 'in', 'choose', 'pick', 'prefer'}
# before the family name: "use / in / with / set it in ... <Name> font"
FONT_ASK = {'use', 'using', 'in', 'with', 'set', 'also', 'choose', 'pick', 'prefer', 'and'}
FONT_NAMED = re.compile(r'\bfont\s+(?:called|named)\s+([A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,2})', re.I)


def _number(s: str) -> int | None:
    s = s.lower().replace('-', ' ').strip()
    if s.isdigit():
        return int(s)
    parts = s.split()
    if len(parts) == 2 and parts[0] in TENS and parts[1] in NUMBER_WORDS:
        return TENS[parts[0]] + NUMBER_WORDS.index(parts[1])
    if s in TENS:
        return TENS[s]
    return NUMBER_WORDS.index(s) if s in NUMBER_WORDS else None


def _count(rx: re.Pattern, text: str, cap: int) -> tuple[tuple[int, int] | None, bool]:
    """(lo, hi) from the first "N pages" / "N-M slides" in the text, capped at `cap`; and whether it was capped."""
    for m in rx.finditer(text):
        lo = _number(m.group('lo'))
        hi = _number(m.group('hi')) if m.group('hi') else lo
        if not lo or not hi:
            continue
        lo, hi = min(lo, hi), max(lo, hi)
        capped = hi > cap
        return (min(lo, cap), min(hi, cap)), capped
    return None, False


def font_of(text: str) -> str | None:
    """The font family a request names, lowercased: a known family as a whole phrase, else up to three words right
    before "font" (stopping at a filler word) or right after "font called/named"."""
    orig = ' '.join(str(text or '').split())
    low = orig.lower()
    for name in KNOWN_FONTS:
        if re.search(rf'(?<![\w-]){re.escape(name)}(?![\w-])', low):
            return name
    m = FONT_NAMED.search(low)
    if m:
        words = [w for w in m.group(1).split() if w not in FONT_FILLER]
        if words and not all(w in FONT_DESCRIBERS for w in words):
            return ' '.join(words[:3])
    for m in FONT_BEFORE.finditer(low):
        before = list(re.finditer(r"[a-z0-9][\w'-]*", low[:m.start()]))
        picked, stop = [], None
        for w in reversed(before):
            if w.group(0) in FONT_FILLER or w.group(0) in FONT_STOP or len(picked) == 3:
                stop = w.group(0)
                break
            picked.insert(0, w)
        while picked and picked[0].group(0) in FONT_DESCRIBERS:
            picked.pop(0)
        if not picked or all(w.group(0) in FONT_DESCRIBERS for w in picked):
            continue
        # a family name: asked for with use/in/with/set..., or written with capitals ("the text also Acme Grotesk font")
        capitalised = all(orig[w.start()].isupper() for w in picked if w.start() < len(orig))
        if stop in FONT_ASK or capitalised:
            return ' '.join(w.group(0) for w in picked)
    return None


def theme_of(text: str) -> str | None:
    return next((name for name, rx in THEMES.items() if rx.search(str(text or ''))), None)


def parse_brief(text: str) -> Brief:
    """The brief a request's text asks for (no LLM)."""
    from .spec import detect_format
    t = ' '.join(str(text or '').split())
    if not t:
        return Brief()
    pages, capped_p = _count(PAGES, t, MAX_PAGES)
    slides, capped_s = _count(SLIDES, t, MAX_SLIDES)
    images = asked_for(IMAGES, t)
    diagrams = asked_for(DIAGRAMS, t)
    # a Studio kind is asked for by its own name; its words don't also ask for a legacy kind ("a mind map" is a
    # mindmap, not a tree as well)
    studio = [k for k in studio_kinds(t) if k in WRITABLE_KINDS] if diagrams else []
    rest = t
    for k in studio:
        rest = STUDIO_KIND_WORDS[k].sub(' ', rest)
    kinds = [k for k in DIAGRAM_KINDS if KIND_WORDS[k].search(rest)] + studio
    if diagrams and len(kinds) < 2 and MANY_DIAGRAMS.search(t):
        # "multiple diagrams" names no kind: pick the ones most documents can use, so the writer is asked for two
        kinds += [k for k in DIAGRAM_KINDS if k not in kinds][:2 - len(kinds)]
    words = None
    if m := WORDS.search(t):
        words = int(m.group(1).replace(',', ''))
    return Brief(format=detect_format(t), pages=pages, slides=slides, theme=theme_of(t), font=font_of(t),
                 images=images, image_source='web' if images and WEB.search(t) else None, diagrams=diagrams,
                 diagram_kinds=kinds, words=words, capped=capped_p or capped_s)


def merge(query: Brief, step: Brief) -> Brief:
    """The primary file step's brief: the query wins for format, theme, font, pages and slides; images and diagrams are
    asked for when either asks."""
    return Brief(
        format=query.format or step.format,
        pages=query.pages or step.pages,
        slides=query.slides or step.slides,
        theme=query.theme or step.theme,
        font=query.font or step.font,
        images=query.images or step.images,
        image_source=query.image_source or step.image_source,
        diagrams=query.diagrams or step.diagrams,
        diagram_kinds=list(dict.fromkeys([*query.diagram_kinds, *step.diagram_kinds])),
        words=query.words or step.words,
        capped=query.capped or step.capped,
        design=query.design or step.design,
    )


def asks_more(brief: 'Brief | None') -> bool:
    """True when the brief asks for more than a copy of an earlier answer: a long document (3+ pages, 8+ slides,
    1,200+ words), images or diagrams (docs/PLAN-accuracy-v2.md C2)."""
    if brief is None:
        return False
    return bool((brief.pages and brief.pages[0] >= 3) or (brief.slides and brief.slides[0] >= 8) or
                (brief.words and brief.words >= 1200) or brief.images or brief.diagrams)


def diagrams_min(brief: 'Brief | None') -> int:
    """How many diagrams the file should draw: one per named kind, at least one when diagrams were asked for."""
    if brief is None or not brief.diagrams:
        return 0
    return max(1, len(brief.diagram_kinds))


def to_dict(brief: 'Brief | None') -> dict | None:
    """The brief as FileBrief JSON (web/src/protocol.ts)."""
    if brief is None:
        return None
    d = asdict(brief)
    for k in ('pages', 'slides'):
        d[k] = list(d[k]) if d[k] else None
    return d


def from_dict(d: dict | None) -> 'Brief | None':
    if not isinstance(d, dict):
        return None
    known = {k: d.get(k) for k in Brief.__dataclass_fields__ if k in d}
    for k in ('pages', 'slides'):
        if known.get(k):
            known[k] = tuple(known[k])
    known['diagram_kinds'] = [k for k in known.get('diagram_kinds') or [] if k in WRITABLE_KINDS]
    return Brief(**known)


def describe(brief: 'Brief', fmt: str | None = None) -> list[str]:
    """The brief as plain lines for a prompt ("Length: 12-13 pages, about 5,000 words")."""
    out = []
    if brief.pages and fmt != 'pptx':
        lo, hi = brief.pages
        span = f'{lo}' if lo == hi else f'{lo}-{hi}'
        out.append(f'Length: {span} pages, about {round((lo + hi) / 2 * WORDS_PER_PAGE, -2):,.0f} words')
    if brief.slides:
        lo, hi = brief.slides
        out.append(f'Length: {lo if lo == hi else f"{lo}-{hi}"} slides')
    if brief.words and not brief.pages:
        out.append(f'Length: about {brief.words:,} words')
    if brief.diagrams:
        n = diagrams_min(brief)
        kinds = ', '.join(brief.diagram_kinds)
        out.append(f'Include at least {n} diagram{"s" if n > 1 else ""}' + (f': {kinds}' if kinds else
                                                                            ' (for example a timeline, flow, cycle '
                                                                            'or comparison) or chart'))
    if brief.images:
        out.append('Include 4-6 figures: give each a Wikimedia Commons search query and caption')
    if brief.theme == 'mono':
        out.append('Black and white: never rely on colour')
    return out
