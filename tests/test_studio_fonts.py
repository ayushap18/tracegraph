"""Studio foundations (docs/PLAN-designer.md 3.2, 3.3, 3.6 and 9.2): the font manager (open-licence downloads with the
licence read first, the shared cache, proprietary requests mapped to open alternatives with honest notes, real metrics,
offline fallback), the per-file workspace, the eight presets, prompt words, design.md v2 and the DesignSystem builder.

No test touches the network: downloads go through a fake `_get`. The one live test is skipped unless TG_LIVE_FONTS=1.
"""
import io
import json
import os
import re
import time

import pytest

from jevrouter.create import brief as brief_mod
from jevrouter.create import design as dm
from jevrouter.create import fonts as legacy_fonts
from jevrouter.create import themes
from jevrouter.studio import fonts, library, plan, presets, tokens, workspace


# ---------- helpers ----------


def make_ttf(family: str, style: str = 'Regular', advance: int = 500, upm: int = 1000) -> bytes:
    """A tiny real TrueType font: every ASCII letter and digit `advance` units wide, space 250, 'W' 900, a box outline
    for each glyph, ascender 800, descender -200, x-height 480, cap height 700."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    chars = [chr(c) for c in range(0x21, 0x7F)]
    names = ['.notdef', 'space'] + [f'g{ord(c)}' for c in chars]
    fb = FontBuilder(upm, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0x20: 'space', **{ord(c): f'g{ord(c)}' for c in chars}})

    def box():
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 700))
        pen.lineTo((400, 700))
        pen.lineTo((400, 0))
        pen.closePath()
        return pen.glyph()
    glyphs = {n: box() for n in names}
    glyphs['space'] = TTGlyphPen(None).glyph()
    fb.setupGlyf(glyphs)
    metrics = {n: (advance, 50) for n in names}
    metrics['space'] = (250, 0)
    metrics[f'g{ord("W")}'] = (900, 50)
    metrics['.notdef'] = (600, 50)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({'familyName': family, 'styleName': style})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200, sxHeight=480,
                sCapHeight=700, version=4)
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


class FakeWeb:
    """Stands in for fonts._get: URL -> bytes (or an exception to raise); records every URL asked for, in order."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    async def __call__(self, http, url, *, max_bytes):
        self.calls.append(url)
        hit = self.routes.get(url)
        if hit is None:
            raise fonts.FetchError('404')
        if isinstance(hit, Exception):
            raise hit
        if len(hit) > max_bytes:
            raise fonts.FetchError('the file is too large')
        return hit

    def ttf_calls(self):
        return [u for u in self.calls if u.endswith('.ttf')]


def fontsource_routes(family: str, licence='OFL-1.1', weights=(400, 700), styles=('normal', 'italic'),
                      category='sans-serif') -> dict:
    fid = fonts.slug(family)
    meta = {'id': fid, 'family': family, 'license': licence, 'weights': list(weights), 'styles': list(styles),
            'defSubset': 'latin', 'category': category, 'variable': False}
    routes = {f'{fonts.FONTSOURCE_API}/{fid}': json.dumps(meta).encode(),
              f'{fonts.JSDELIVR}/npm/@fontsource/{fid}/LICENSE': b'This Font Software is licensed under the SIL '
                                                                  b'Open Font License, Version 1.1.'}
    for w in weights:
        for st in styles:
            routes[f'{fonts.JSDELIVR}/fontsource/fonts/{fid}@latest/latin-{w}-{st}.ttf'] = make_ttf(
                family, f'{w} {st}', advance=600 if w >= 700 else 500)
    return routes


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An empty font cache, no system fonts, no environment font, and fresh lookup caches."""
    d = tmp_path / 'fonts'
    monkeypatch.setattr(fonts, 'CACHE_DIR', d)
    monkeypatch.setattr(legacy_fonts, 'system_index', lambda: {})
    monkeypatch.delenv('TRACEGRAPH_BODY_FONT', raising=False)
    monkeypatch.setattr(fonts, '_INDEX', {'at': 0.0, 'fonts': None})
    fonts.clear_caches()
    yield d
    fonts.clear_caches()


def web(monkeypatch, routes) -> FakeWeb:
    fake = FakeWeb(routes)
    monkeypatch.setattr(fonts, '_get', fake)
    return fake


HTTP = object()   # a stand-in session: the fake _get never uses it


# ---------- font manager: downloads and licences ----------


async def test_an_open_family_is_downloaded_once_with_its_licence_read_first(cache, monkeypatch):
    fake = web(monkeypatch, fontsource_routes('Poppins'))
    res = await fonts.ensure('Poppins', HTTP)
    assert (res.family, res.source, res.licence, res.embeddable) == ('Poppins', 'fontsource', 'OFL-1.1', True)
    assert set(res.faces) == {'regular', 'bold', 'italic', 'bolditalic'} and res.note is None
    assert res.install_url == 'https://fontsource.org/fonts/poppins'
    # the licence came from the metadata before any font file was fetched
    assert fake.calls[0] == f'{fonts.FONTSOURCE_API}/poppins'
    assert fake.calls.index(fake.ttf_calls()[0]) > 0
    d = cache / 'poppins'
    meta = json.loads((d / 'meta.json').read_text())
    assert meta['licence'] == 'OFL-1.1' and meta['source'] == 'fontsource' and meta['category'] == 'sans'
    assert 'Open Font License' in (d / 'LICENSE').read_text()
    assert sorted(p.name for p in d.glob('*.ttf')) == ['bold.ttf', 'bolditalic.ttf', 'italic.ttf', 'regular.ttf']
    # a second request is served from the cache, with no network at all
    n = len(fake.calls)
    again = await fonts.ensure('poppins', HTTP)
    assert again.source == 'cache' and len(fake.calls) == n
    assert fonts.resolve_local('Poppins').source == 'cache'
    local_offline = await fonts.ensure('Poppins', None)
    assert local_offline.source == 'cache'


async def test_a_family_that_is_not_openly_licensed_is_never_downloaded(cache, monkeypatch):
    fake = web(monkeypatch, fontsource_routes('Brandy Sans', licence='Proprietary'))
    res = await fonts.ensure('Brandy Sans', HTTP)
    assert fake.ttf_calls() == []                     # nothing but the metadata was fetched
    assert not (cache / 'brandysans').exists()
    assert res.source == 'builtin' and res.family == 'Helvetica' and not res.embeddable
    assert res.note == ("Brandy Sans uses the Proprietary licence, which isn't an open font licence, so it wasn't "
                        "downloaded, so this uses Helvetica.")
    for bad in ('MIT', 'CC0-1.0', 'Unlicense', 'unknown', ''):
        assert fonts.normalise_licence(bad) not in fonts.LICENCES_OK
    assert [fonts.normalise_licence(x) for x in ('OFL', 'ofl-1.1', 'SIL Open Font License 1.1', {'id': 'Apache-2.0'},
                                                 'APACHE2', 'UFL')] == ['OFL-1.1', 'OFL-1.1', 'OFL-1.1', 'Apache-2.0',
                                                                        'Apache-2.0', 'UFL-1.0']


async def test_google_fonts_is_the_second_source_and_its_licence_is_checked(cache, monkeypatch):
    md = ('name: "Gamma Grotesk"\ndesigner: "X"\nlicense: "APACHE2"\ncategory: "SANS_SERIF"\n'
          'fonts {\n  name: "Gamma Grotesk"\n  style: "normal"\n  weight: 400\n  filename: "GammaGrotesk-Regular.ttf"\n}\n'
          'fonts {\n  name: "Gamma Grotesk"\n  style: "normal"\n  weight: 700\n  filename: "GammaGrotesk-Bold.ttf"\n}\n')
    base = f'{fonts.GOOGLE_FONTS}/apache/gammagrotesk'
    fake = web(monkeypatch, {f'{base}/METADATA.pb': md.encode(), f'{base}/GammaGrotesk-Regular.ttf': make_ttf('Gamma Grotesk'),
                             f'{base}/GammaGrotesk-Bold.ttf': make_ttf('Gamma Grotesk', 'Bold', 600),
                             f'{base}/LICENSE.txt': b'Apache License 2.0'})
    res = await fonts.ensure('Gamma Grotesk', HTTP)
    assert (res.source, res.licence, set(res.faces)) == ('google-fonts', 'Apache-2.0', {'regular', 'bold'})
    assert fake.calls[0].startswith(fonts.FONTSOURCE_API)          # Fontsource first
    meta_at = fake.calls.index(f'{base}/METADATA.pb')
    assert all(fake.calls.index(u) > meta_at for u in fake.ttf_calls())
    assert (cache / 'gammagrotesk' / 'LICENSE').read_text() == 'Apache License 2.0'
    # a METADATA.pb naming another licence stops before any file
    other = md.replace('APACHE2', 'OTHER').replace('Gamma Grotesk', 'Delta Grotesk').replace('GammaGrotesk',
                                                                                             'DeltaGrotesk')
    fake2 = web(monkeypatch, {f'{fonts.GOOGLE_FONTS}/ofl/deltagrotesk/METADATA.pb': other.encode(),
                              f'{fonts.GOOGLE_FONTS}/ofl/deltagrotesk/DeltaGrotesk-Regular.ttf': make_ttf('D')})
    res2 = await fonts.ensure('Delta Grotesk', HTTP)
    assert fake2.ttf_calls() == [] and res2.source == 'builtin' and 'OTHER' in res2.note


def test_proprietary_requests_map_to_open_alternatives_with_honest_notes():
    assert fonts.alternative('Anthropic Sans') == (
        'Inter', "Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match.")
    assert fonts.alternative('Helvetica Neue')[0] == 'Inter'
    assert fonts.alternative('SF Pro')[0] == 'Inter' and fonts.alternative('sf-pro')[0] == 'Inter'
    assert fonts.alternative('Segoe UI')[0] == 'Open Sans'
    assert fonts.alternative('calibri') == (
        'Carlito', "Calibri isn't openly licensed, so this uses Carlito, an open font with the same letter widths.")
    assert fonts.alternative('Inter') is None and fonts.alternative('Lexend') is None
    assert fonts.alternative('Acme Grotesk') is None          # unknown: ensure() tries a download
    # every alternative is itself an open family the catalogue knows
    for target in fonts.SIMILAR.values():
        assert fonts.key(target) in {fonts.key(f) for f in fonts.CATALOG}, target
        assert fonts.CATALOG[fonts.display(target)][1] in fonts.LICENCES_OK
    for note in (fonts.alternative(k)[1] for k in fonts.SIMILAR):
        assert '\u2014' not in note and '\u2013' not in note


async def test_a_proprietary_request_downloads_the_open_alternative(cache, monkeypatch):
    fake = web(monkeypatch, fontsource_routes('Inter', weights=(400, 700)))
    res = await fonts.ensure('Anthropic Sans', HTTP)
    assert (res.requested, res.family, res.source) == ('Anthropic Sans', 'Inter', 'fontsource')
    assert res.note == "Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match."
    assert not any('anthropic' in u for u in fake.calls)
    assert not (cache / 'anthropicsans').exists()


async def test_offline_and_failures_fall_back_to_installed_fonts_and_never_raise(cache, monkeypatch, tmp_path):
    arial = tmp_path / 'Arial.ttf'
    arial.write_bytes(make_ttf('Arial'))
    monkeypatch.setattr(legacy_fonts, 'system_index', lambda: {'arial': {'regular': str(arial)}})
    fonts.clear_caches()
    fake = web(monkeypatch, {})
    res = await fonts.ensure('Poppins', None)                 # offline: no http at all
    assert fake.calls == []
    assert (res.family, res.source) == ('Arial', 'system')
    assert res.note == "Poppins isn't installed and can't be downloaded here (offline), so this uses Arial."
    # every source failing, or the fetcher blowing up, still gives a usable font
    res2 = await fonts.ensure('Poppins', HTTP)
    assert res2.family == 'Arial' and res2.note
    monkeypatch.setattr(fonts, '_get', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    res3 = await fonts.ensure('Poppins', HTTP)
    assert res3.family == 'Arial'
    # with nothing installed at all: the PDF standard font of the category
    monkeypatch.setattr(legacy_fonts, 'system_index', lambda: {})
    fonts.clear_caches()
    serif = await fonts.ensure('Merriweather', None)
    assert (serif.family, serif.source, serif.faces, serif.office_fallback) == ('Times-Roman', 'builtin', {}, 'Georgia')
    assert (await fonts.ensure('', None)).family == 'Helvetica'
    assert (await fonts.ensure('JetBrains Mono', None)).family == 'Courier'


async def test_the_system_font_of_the_request_is_used_when_installed(cache, monkeypatch, tmp_path):
    p = tmp_path / 'Lexend.ttf'
    p.write_bytes(make_ttf('Lexend'))
    monkeypatch.setattr(legacy_fonts, 'system_index', lambda: {'lexend': {'regular': str(p)}})
    fonts.clear_caches()
    fake = web(monkeypatch, {})
    res = await fonts.ensure('Lexend', HTTP)
    assert (res.family, res.source, res.licence, res.note) == ('Lexend', 'system', 'system', None)
    assert fake.calls == []


async def test_the_users_own_font_file_is_used_as_is(cache, monkeypatch, tmp_path):
    p = tmp_path / 'house.ttf'
    p.write_bytes(make_ttf('House Sans'))
    monkeypatch.setenv('TRACEGRAPH_BODY_FONT', str(p))
    fonts.clear_caches()
    fake = web(monkeypatch, {})
    res = await fonts.ensure('House Sans', HTTP)
    assert (res.family, res.source, res.licence, res.embeddable) == ('House Sans', 'user', 'user', True)
    assert fake.calls == []


async def test_downloads_pass_the_egress_guard():
    for url in ('file:///etc/passwd', 'ftp://example.com/x.ttf', 'http://127.0.0.1/x.ttf', 'http://localhost/x.ttf',
                'http://10.0.0.8/x.ttf', 'https://user:pw@example.com/x.ttf', 'http://example.com:8777/x.ttf'):
        with pytest.raises(fonts.FetchError):
            await fonts._get(object(), url, max_bytes=100)


async def test_a_file_that_is_not_a_font_is_refused(cache, monkeypatch):
    routes = fontsource_routes('Poppins', weights=(400,), styles=('normal',))
    routes[f'{fonts.JSDELIVR}/fontsource/fonts/poppins@latest/latin-400-normal.ttf'] = b'<html>not a font</html>'
    web(monkeypatch, routes)
    res = await fonts.ensure('Poppins', HTTP)
    assert res.source == 'builtin' and not (cache / 'poppins' / 'meta.json').exists()


# ---------- metrics, measuring and wrapping ----------


@pytest.fixture
def testfont(cache):
    """'Test Sans' in the cache: letters 500 units, space 250, W 900 (1000 units per em); regular face only."""
    fonts._store('Test Sans', 'OFL-1.1', 'fontsource', 'sans', {'regular': make_ttf('Test Sans')}, 'OFL', {})
    return 'Test Sans'


def test_metrics_come_from_the_font_file(testfont, cache):
    m = fonts.metrics(str(cache / 'testsans' / 'regular.ttf'))
    assert (m.units_per_em, m.ascender, m.descender, m.x_height, m.cap_height) == (1000, 800, -200, 480, 700)
    assert m.widths[ord('a')] == 500 and m.widths[32] == 250 and m.widths[ord('W')] == 900
    assert m.advance('aW a', 10) == pytest.approx((500 + 900 + 250 + 500) / 100)
    assert fonts.line_metrics(testfont, 10) == pytest.approx((8.0, 2.0, 12.0))
    assert fonts.line_metrics(testfont, 10, 1.5)[2] == 15.0
    assert fonts.x_height(testfont, 10) == pytest.approx(4.8)


def test_measure_uses_real_widths_and_the_wider_office_fallback(testfont):
    assert fonts.measure('ab', testfont, 10, fmt='pdf') == pytest.approx(10.0)
    assert fonts.measure('', testfont, 10) == 0.0
    # bold without a bold face: the regular widths made 5% wider, as synthetic bold is drawn
    assert fonts.measure('ab', testfont, 10, bold=True, fmt='pdf') == pytest.approx(10.5)
    # PowerPoint names the font; where it isn't installed it shows Calibri, so the wider of the two is used
    assert fonts.office_fallback(testfont) == 'Calibri'
    from reportlab.pdfbase.pdfmetrics import stringWidth
    word = 'iiii'   # narrow in Helvetica (the stand-in for Calibri here), 500 units each in Test Sans
    assert fonts.measure(word, testfont, 10, fmt='pptx') == pytest.approx(20.0)
    wide = 'MMMM'
    assert fonts.measure(wide, testfont, 10, fmt='pptx') == pytest.approx(max(20.0, stringWidth(wide, 'Helvetica', 10)))
    assert fonts.measure(wide, testfont, 10, fmt='pdf') == pytest.approx(20.0)


def test_an_unknown_family_is_measured_as_its_categorys_standard_font(cache):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    assert fonts.measure('Hello there', 'Nope Sans', 12, fmt='pdf') == pytest.approx(
        stringWidth('Hello there', 'Helvetica', 12))
    assert fonts.measure('Hello', 'Nope Serif', 12, fmt='pdf') == pytest.approx(stringWidth('Hello', 'Times-Roman', 12))
    assert fonts.measure('Hello', 'Nope Mono', 12, bold=True, fmt='pdf') == pytest.approx(
        stringWidth('Hello', 'Courier-Bold', 12))
    asc, desc, adv = fonts.line_metrics('Nope Sans', 10)
    assert asc > 0 and desc > 0 and adv == 12.0


def test_wrap_greedy_with_long_words_broken(testfont):
    # each letter 5 pt at 10 pt, space 2.5 pt: 'aaaa bbbb' is 42.5 pt
    assert fonts.wrap('aaaa bbbb cccc', testfont, 10, 45, fmt='pdf') == ['aaaa bbbb', 'cccc']
    assert fonts.wrap('aaaa bbbb cccc', testfont, 10, 1000, fmt='pdf') == ['aaaa bbbb cccc']
    assert fonts.wrap('abcdefghij', testfont, 10, 20, fmt='pdf') == ['abcd', 'efgh', 'ij']
    assert fonts.wrap('ab\n\ncd', testfont, 10, 100, fmt='pdf') == ['ab', '', 'cd']
    assert fonts.wrap('', testfont, 10, 100) == [] and fonts.wrap('   ', testfont, 10, 100) == []
    for line in fonts.wrap('The quick brown fox jumps over the lazy dog ' * 5, testfont, 18, 300):
        assert fonts.measure(line, testfont, 18) <= 300


def test_subset_for_pdf_keeps_only_the_glyphs_needed(testfont, cache):
    from fontTools.ttLib import TTFont
    path = cache / 'testsans' / 'regular.ttf'
    data = fonts.subset_for_pdf(str(path), 'abc')
    assert len(data) < path.stat().st_size
    with TTFont(io.BytesIO(data)) as f:
        cmap = f.getBestCmap()
        assert {ord('a'), ord('b'), ord('c'), 32} <= set(cmap) and ord('z') not in cmap


def test_preview_png_for_cached_open_families_only(testfont):
    png = fonts.preview_png(testfont, 'Aa', 24)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert fonts.preview_png('Not Here') is None


def test_prune_evicts_least_recently_used_families(cache):
    now = time.time()
    for i, name in enumerate(('old', 'mid', 'new')):
        d = cache / name
        d.mkdir(parents=True)
        (d / 'regular.ttf').write_bytes(b'x' * 1000)
        (d / 'meta.json').write_text('{}')
        os.utime(d / 'meta.json', (now - 1000 + i * 100, now - 1000 + i * 100))
    (cache / '_index.json').write_text('[]')
    freed = fonts.prune(2100)
    assert freed >= 1000 and not (cache / 'old').exists() and (cache / 'mid').exists() and (cache / 'new').exists()
    assert (cache / '_index.json').exists()
    assert fonts.prune(10 ** 9) == 0
    assert fonts.prune(0) > 0 and not any(p.is_dir() for p in cache.iterdir())


# ---------- search ----------


async def test_search_offline_lists_only_open_families(cache):
    found, offline = await fonts.search('lex', None)
    assert offline and found[0].family == 'Lexend' and found[0].licence == 'OFL-1.1'
    assert found[0].preview == '/api/fonts/preview?family=Lexend' and not found[0].installed
    curated, _ = await fonts.search('', None, limit=50)
    assert len(curated) == 50 and all(f.licence in fonts.LICENCES_OK for f in curated)
    assert {'Atkinson Hyperlegible', 'Lexend', 'Inter'} <= {f.family for f in curated}
    first, _ = await fonts.search('Helvetica Neue', None)
    assert first == [] or first[0].family == 'Inter'
    helv, _ = await fonts.search('helvetica', None)
    assert helv and helv[0].family == 'Inter'      # the proprietary name finds its open alternative first


async def test_search_online_filters_licences_and_icon_fonts(cache, monkeypatch):
    index = [{'id': 'lexend', 'family': 'Lexend', 'license': 'OFL-1.1', 'category': 'sans-serif', 'weights': [400, 700],
              'styles': ['normal']},
             {'id': 'lexend-deca', 'family': 'Lexend Deca', 'license': 'OFL-1.1', 'category': 'sans-serif',
              'weights': [400], 'styles': ['normal']},
             {'id': 'lexmark', 'family': 'Lexmark', 'license': 'MIT', 'category': 'sans-serif', 'weights': [400],
              'styles': ['normal']},
             {'id': 'lexicons', 'family': 'Lexicons', 'license': 'OFL-1.1', 'category': 'icons', 'weights': [400],
              'styles': ['normal']}]
    fake = web(monkeypatch, {fonts.FONTSOURCE_API: json.dumps(index).encode()})
    found, offline = await fonts.search('lex', HTTP)
    assert not offline and [f.family for f in found] == ['Lexend', 'Lexend Deca']
    assert found[0].styles == ['regular', 'bold'] and found[0].category == 'sans'
    await fonts.search('lexend', HTTP)
    assert len(fake.calls) == 1                                    # the list is fetched once, then remembered
    assert json.loads((cache / '_index.json').read_text())[0]['family'] == 'Lexend'
    monkeypatch.setattr(fonts, '_INDEX', {'at': 0.0, 'fonts': None})
    (cache / '_index.json').unlink()
    web(monkeypatch, {})
    found, offline = await fonts.search('lex', HTTP)                # the API failing: the offline list
    assert offline and found[0].family == 'Lexend'


# ---------- the legacy adapter (create/fonts.py) ----------


async def test_legacy_resolve_embeds_a_font_studio_has_cached(cache, monkeypatch):
    web(monkeypatch, fontsource_routes('Poppins'))
    await fonts.ensure('Poppins', HTTP)
    choice = legacy_fonts.resolve('poppins', 'pdf')
    assert choice.embedded and choice.used == 'Poppins' and choice.regular.endswith('regular.ttf')
    assert choice.bold.endswith('bold.ttf') and choice.note is None
    # a design stack naming it embeds it too
    assert legacy_fonts.resolve(None, 'pdf', stack=['Poppins', 'sans-serif']).used == 'Poppins'
    # Office files still name the font as before, and a proprietary request keeps today's honest note
    assert legacy_fonts.resolve('poppins', 'docx').used == 'Poppins'
    assert legacy_fonts.resolve('anthropic sans', 'pdf').used == 'Helvetica'
    assert legacy_fonts.studio_cached('anthropic sans') is None


# ---------- workspace ----------


@pytest.fixture
def design_dir(tmp_path, monkeypatch):
    d = tmp_path / 'design'
    monkeypatch.setattr(workspace, 'DESIGN_DIR', d)
    monkeypatch.setattr(workspace, 'SANDBOX_DIR', tmp_path / 'sandboxes')
    return d


def _plan(fid='0123456789ab', score=90) -> plan.DesignPlan:
    system = json.loads(json.dumps(presets.get('minimal').to_dict()))   # as it comes back from plan.json
    return plan.DesignPlan(file_id=fid, format='pptx', preset='minimal', system=system,
                           direction={'preset': 'minimal', 'mood': [], 'dark': None, 'pages': []}, score=score)


def test_workspace_assets_refs_and_manifest(design_dir):
    ws = workspace.Workspace('0123456789ab')
    assert not ws.exists() and ws.path == design_dir / '0123456789ab'
    ref = ws.put('images', 'cover.png', b'png-bytes')
    assert ref == 'ws:images/cover.png' and ws.exists()
    assert ws.get(ref) == b'png-bytes' and ws.path_of(ref) == ws.path / 'images' / 'cover.png'
    assert ws.get('ws:images/missing.png') is None and ws.get('nonsense') is None
    for bad in ('ws:images/../plan.json', 'ws:plans/x.png', 'ws:images/Cover.png', 'asset:abc', 'ws:images/',
                'ws:images/a/b.png', 'ws:thumbs/.hidden'):
        with pytest.raises(ValueError):
            ws.path_of(bad)
    with pytest.raises(ValueError):
        ws.put('images', '../escape.png', b'x')
    with pytest.raises(ValueError):
        workspace.Workspace('../etc')
    ws.put('text', 'measure.json', b'{}')
    m = ws.manifest()
    assert m['file_id'] == '0123456789ab' and m['bytes'] == 11
    assert m['files']['ws:images/cover.png'] == {'bytes': 9, 'sha256': __import__('hashlib').sha256(
        b'png-bytes').hexdigest()}
    assert m['created'] <= m['touched']


def test_workspace_plan_history_report_thumbs_and_events(design_dir):
    ws = workspace.Workspace('0123456789ab')
    assert ws.load_plan() is None and ws.load_report() is None and ws.thumbs() == [] and ws.events() == []
    ws.save_plan(_plan(score=70))
    ws.save_plan(_plan(score=90))
    assert ws.load_plan().score == 90 and ws.load_plan(previous=True).score == 70
    assert ws.load_plan() == _plan(score=90)                     # plan <-> JSON round-trips exactly
    ws.save_report({'score': 90, 'checks': []})
    assert ws.load_report() == {'score': 90, 'checks': []}
    for page in (2, 0, 1):
        assert ws.save_thumb(page, b'\x89PNG') == f'ws:thumbs/{page + 1:03d}.png'
    assert [p.name for p in ws.thumbs()] == ['001.png', '002.png', '003.png']
    ws.log({'phase': 'direct', 'source': 'keyless'})
    ws.log({'phase': 'qa', 'score': 90})
    evs = ws.events()
    assert [e['phase'] for e in evs] == ['direct', 'qa'] and all('t' in e for e in evs)
    (ws.path / 'plan.json').write_text('{broken')
    assert ws.load_plan() is None


def test_workspace_copy_delete_and_sandboxes(design_dir, tmp_path):
    ws = workspace.open_workspace('0123456789ab')
    ws.put('images', 'a.png', b'a')
    ws.put('diagrams', 'd.png', b'd')
    ws.save_plan(_plan())
    ws.save_thumb(0, b't')
    new = ws.copy_to('fedcba987654')
    assert new.get('ws:images/a.png') == b'a' and new.get('ws:diagrams/d.png') == b'd'
    assert new.load_plan() is None and new.thumbs() == []          # the new file lays out and renders its own
    assert new.events()[-1]['phase'] == 'copy' and new.events()[-1]['from'] == '0123456789ab'
    assert workspace.Workspace('aaaaaaaaaaaa').copy_to('bbbbbbbbbbbb').exists() is False
    workspace.drop('0123456789ab')
    assert not ws.exists() and new.exists()
    workspace.drop('../../nope')                                    # a bad id is ignored, nothing outside is touched
    sb = workspace.open_workspace('0123456789ab', sandbox='sb1')
    sb.put('art', 'x.png', b'x')
    assert sb.path == tmp_path / 'sandboxes' / 'sb1' / '0123456789ab' and sb.exists()
    workspace.drop_sandbox('sb1')
    assert not sb.exists()
    with pytest.raises(ValueError):
        workspace.open_workspace('0123456789ab', sandbox='../x')


def test_workspace_prune_ttl_then_lru(design_dir):
    now = time.time()
    spaces = {}
    for i, fid in enumerate(('aaaaaaaaaaa1', 'aaaaaaaaaaa2', 'aaaaaaaaaaa3', 'aaaaaaaaaaa4')):
        ws = workspace.Workspace(fid)
        ws.put('images', 'x.png', b'x' * 1000)
        spaces[fid] = ws
    ages = {'aaaaaaaaaaa1': 20 * 86400, 'aaaaaaaaaaa2': 3000, 'aaaaaaaaaaa3': 2000, 'aaaaaaaaaaa4': 1000}
    for fid, age in ages.items():
        mpath = spaces[fid].path / 'manifest.json'
        m = json.loads(mpath.read_text())
        m['touched'] = now - age
        mpath.write_text(json.dumps(m))
        os.utime(mpath, (now - age, now - age))
        os.utime(spaces[fid].path, (now - age, now - age))
    first = spaces['aaaaaaaaaaa1'].size_bytes()
    freed = workspace.prune(cap_bytes=10 ** 9, ttl_days=14)
    assert freed == first and not spaces['aaaaaaaaaaa1'].exists()
    total = sum(spaces[f].size_bytes() for f in ('aaaaaaaaaaa2', 'aaaaaaaaaaa3', 'aaaaaaaaaaa4'))
    freed = workspace.prune(cap_bytes=total - 1, ttl_days=14)
    assert freed > 0 and not spaces['aaaaaaaaaaa2'].exists()          # the least recently used goes first
    assert spaces['aaaaaaaaaaa3'].exists() and spaces['aaaaaaaaaaa4'].exists()
    # a read counts as use
    spaces['aaaaaaaaaaa3'].get('ws:images/x.png')
    assert spaces['aaaaaaaaaaa3'].last_used() > spaces['aaaaaaaaaaa4'].last_used()


# ---------- presets and templates ----------


def test_every_preset_is_a_complete_readable_design_system():
    for pid in presets.PRESETS:
        ds = presets.get(pid)
        assert ds.id == pid and set(ds.colors) == set(tokens.COLOR_ROLES)
        assert all(re.fullmatch(r'[0-9A-F]{6}', v) for v in ds.colors.values())
        assert len(ds.chart_palette) == 6 and len(set(ds.chart_palette)) == 6
        c = ds.colors
        for fg, backs, ratio in tokens.TEXT_RULES:
            for b in backs:
                assert themes.contrast(c[fg], c[b]) >= ratio - 1e-9, (pid, fg, b)
        assert themes.contrast(c['on_accent'], c['accent']) >= 4.5, pid
        for col in ds.chart_palette:
            assert themes.contrast(col, c['bg']) >= 3.0, (pid, col)
        worst = 'FFFFFF' if themes._lum(c['overlay']) < 0.18 else '000000'
        assert themes.contrast(c['text'], themes.mix(worst, c['overlay'], ds.overlay_alpha)) >= 4.5, pid
        assert 1.2 <= ds.scale.ratio <= 1.5 and ds.image in tokens.IMAGE_TREATMENTS
        assert ds.shape.accent_shape in tokens.ACCENT_SHAPES
        for fam in (ds.families.display, ds.families.heading, ds.families.body, ds.families.caption, ds.families.mono):
            assert fam in fonts.CATALOG, (pid, fam)              # every preset font is openly licensed
        assert tokens.DesignSystem.from_dict(json.loads(json.dumps(ds.to_dict()))) == ds
        assert ds.dark == (pid == 'bold-dark')
    assert presets.get('nope').id == presets.DEFAULT_PRESET and presets.get('dark').id == 'bold-dark'


def test_presets_for_students_have_their_character():
    hl = presets.get('high-legibility')
    assert hl.families.body == 'Atkinson Hyperlegible' and hl.families.heading == 'Lexend' and not hl.justify
    assert hl.size('body', 'pptx') >= 22 and hl.size('body', 'pdf') >= 12 and hl.scale.line_height >= 1.5
    mono = presets.get('mono')
    assert mono.hatch and mono.grey_images
    assert all(abs(int(x[0:2], 16) - int(x[2:4], 16)) < 3 for x in mono.colors.values())      # greys only
    assert presets.get('editorial').families.display == 'Playfair Display'
    assert presets.get('academic').justify and presets.get('vibrant').shape.radius > presets.get('minimal').shape.radius


def test_preset_and_template_listings():
    infos = presets.list_presets()
    assert [p.id for p in infos] == list(presets.PRESETS)
    bd = infos[0].to_dict()
    assert bd['thumb'] == '/api/design/presets/bold-dark/thumb' and bd['dark'] and set(bd['colors']) == {
        'bg', 'text', 'accent', 'accent2'}
    assert bd['families']['body'] == 'Inter'
    temps = presets.list_templates()
    assert [t.id for t in temps] == list(presets.TEMPLATES)
    for t in temps:
        assert t.preset in presets.PRESETS and t.tone and t.format in ('pptx', 'pdf', 'docx')
        assert set(t.sequence) <= set(library.for_format(t.format)), t.id
        assert (t.paper is None) == (t.format == 'pptx')
        for line in (t.name, t.description, *t.tone):
            assert '\u2014' not in line and '\u2013' not in line
    assert presets.template('lab-report').preset == 'academic' and presets.template('nope') is None
    temps[0].tone.append('x')
    assert 'x' not in presets.template(temps[0].id).tone                   # callers get copies


# ---------- prompt words ----------


@pytest.mark.parametrize('text,preset,dark', [
    ('a 12 slide PPT about space, bold and creative with a dark design, a strong cover', 'bold-dark', True),  # 2808
    ('make it cinematic', 'bold-dark', None),
    ('an elegant essay on Keats', 'editorial', None),
    ('clean look, sleek slides', 'minimal', None),
    ('a fun colourful deck for kids', 'vibrant', None),
    ('revision notes in soft colours', 'pastel', None),
    ('a lab report on enzymes', 'academic', None),
    ('black and white so I can photocopy it', 'mono', None),
    ('easy to read, my brother is dyslexic, make it playful', 'high-legibility', None),
    ('bold and fun but print-friendly please', 'mono', None),          # print words beat mood words
    ('the editorial preset in dark mode', 'editorial', True),           # any other preset + dark
    ('pastel with a white background', 'pastel', False),
    ('use the high legibility preset', 'high-legibility', None),
    ('a presentation on the Dark Ages with fun facts', None, None),     # topics, not styles
    ('a report in a sans serif font about dark matter', None, None),
    ('', None, None),
])
def test_read_prompt(text, preset, dark):
    s = presets.read_prompt(text)
    assert (s.preset, s.dark) == (preset, dark)


def test_read_prompt_details():
    s = presets.read_prompt('bold and creative with a dark design')
    assert s.mood == ['vibrant', 'dark'] and 'dark design' in s.words
    assert presets.read_prompt('black and white').print_version
    assert presets.read_prompt('headings in Poppins and body text in Lora').fonts == {
        'heading': 'Poppins', 'body': 'Lora', 'display': 'Poppins'}
    assert presets.read_prompt('use Lexend font').fonts == {'body': 'Lexend'}
    assert presets.read_prompt('titles in French').fonts is None
    assert presets.read_prompt('a minimal deck, dyslexia friendly').preset == 'high-legibility'


def test_the_brief_reads_dark_design_as_the_dark_theme():
    assert brief_mod.parse_brief('a 12 slide PPT, bold and creative with a dark design').theme == 'dark'
    for t in ('dark slides please', 'dark-themed deck', 'a dark look', 'in dark mode'):
        assert brief_mod.theme_of(t) == 'dark', t
    for t in ('the Dark Ages', 'dark matter and dark energy', 'a report on dark chocolate'):
        assert brief_mod.theme_of(t) is None, t
    assert brief_mod.font_of('use Helvetica Neue font') == 'helvetica neue'
    assert brief_mod.font_of('slides in Poppins') == 'poppins'


# ---------- building the DesignSystem ----------


def test_build_system_priority_and_run_2808():
    ds = tokens.build_system(prompt='a 12 slide PPT, bold and creative with a dark design')
    assert ds.id == 'bold-dark' and ds.dark and themes._lum(ds.colors['bg']) < 0.05
    assert ds.source[0].startswith('prompt:')
    assert tokens.build_system().id == presets.DEFAULT_PRESET and tokens.build_system().source == ['default']
    assert tokens.build_system(preset='academic').source == ['preset:academic']
    assert tokens.build_system(preset='warm').id == 'editorial'                    # a legacy theme name
    assert tokens.build_system(preset='academic', prompt='make it playful').id == 'vibrant'   # prompt > preset
    minimal_dark = tokens.build_system(prompt='a minimal deck in dark mode')
    assert minimal_dark.id == 'minimal' and minimal_dark.dark and minimal_dark.name == 'Minimal (dark)'
    forced = tokens.build_system(prompt='a dark design', dark=False)
    assert not forced.dark and forced.colors['bg'] == 'FFFFFF'
    for junk in (dict(preset=42, design='x', prompt=None, font=3), dict(design={'colors': 'no'}),
                 dict(design={'colors': {'bg': 'zzzzzz'}, 'system': {'scale_ratio': 'big'}})):
        ds = tokens.build_system(**junk)
        assert ds.id in presets.PRESETS


def test_build_system_fonts_and_honest_notes():
    ds = tokens.build_system(prompt='slides in anthropic sans font', font='anthropic sans')
    assert ds.families.body == 'Inter'
    assert ds.notes == ["Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match."]
    ds = tokens.build_system(prompt='headings in Poppins', font=None)
    assert ds.families.heading == 'Poppins' and ds.families.display == 'Poppins' and ds.notes == []
    ds = tokens.build_system(prompt='dyslexia friendly handout')
    assert ds.families.body == 'Atkinson Hyperlegible' and not ds.justify


DESIGN_MD = """# Acme design system
## Colours
- Page background: #0B1020
- Body text: #3A3F4B
- Headings: #FFFFFF
- Accent: #FF7A45
## Typography
- Headings: Space Grotesk, sans-serif
- Body: Helvetica Neue, Arial, sans-serif
- Type scale: major third
## Spacing
- Base unit: 4px
- Border radius: 16px
## Imagery
- Use rounded photos with a soft shadow
## Do's and Don'ts
- Do: keep one idea per slide
- Don't use more than two typefaces
- Avoid clip art
"""


def test_design_md_v2_fields():
    tok = dm.parse_design(DESIGN_MD, 'design.md')
    assert (tok.scale_ratio, tok.spacing_unit, tok.image, tok.radius) == (1.25, 4.0, 'rounded', 16)
    assert tok.do == ['keep one idea per slide']
    assert tok.dont == ['use more than two typefaces', 'clip art']
    spec = dm.to_spec(tok)
    assert spec['system'] == {'scale_ratio': 1.25, 'spacing_unit': 4.0, 'image': 'rounded',
                              'do': ['keep one idea per slide'], 'dont': ['use more than two typefaces', 'clip art'],
                              'radius': 16}
    assert dm.clean_design(spec)['system'] == spec['system']
    # a design without v2 lines gives exactly today's spec (no 'system' key)
    plain = dm.to_spec(dm.parse_design('Background: #FFFFFF\nText: #111111\nHeadings: #0B3D91', 'd.md'))
    assert 'system' not in plain and 'system' not in dm.clean_design(plain)
    bad = dm.clean_design({'colors': {'bg': 'FFFFFF'}, 'system': {'scale_ratio': 9, 'spacing_unit': True,
                                                                  'image': 'sparkly', 'do': 'x', 'dont': [3, '<b>ok']}})
    assert bad['system'] == {'dont': ['b ok']}
    assert dm.clean_design({'colors': {'bg': 'FFFFFF'}, 'system': 'nope'}).get('system') is None


def test_build_system_from_design_md_keeps_contrast():
    spec = dm.to_spec(dm.parse_design(DESIGN_MD, 'design.md'))
    ds = tokens.build_system(design=spec, prompt='a light pastel deck with a white background')
    assert ds.source[0] == 'design.md' and ds.dark                           # design.md beats prompt words
    assert ds.colors['bg'] == '0B1020' and ds.colors['accent'] == 'FF7A45'
    assert themes.contrast(ds.colors['text'], ds.colors['bg']) >= 4.5         # #3A3F4B on navy was repaired (A4)
    assert 'The body text colour was lightened slightly to stay readable on its background.' in ds.notes or \
        'The body text colour was changed to stay readable on its background.' in ds.notes
    assert ds.families.heading == 'Space Grotesk' and ds.families.body == 'Inter'
    assert "Helvetica Neue isn't openly licensed, so this uses Inter, the closest open match." in ds.notes
    assert (ds.scale.ratio, ds.spacing.unit, ds.shape.radius, ds.image) == (1.25, 4.0, 16.0, 'rounded')
    assert ds.do == ['keep one idea per slide'] and len(ds.dont) == 2
    over = tokens.from_design(dm.clean_design(spec))
    assert over['colors']['bg'] == '0B1020' and over['families']['body'] == 'Helvetica Neue'
    assert tokens.from_design({}) == {} and tokens.from_design('x') == {}
    for note in ds.notes:
        assert '\u2014' not in note and '\u2013' not in note


def test_with_dark_print_version_and_legacy_theme():
    for pid in presets.PRESETS:
        base = presets.get(pid)
        for dark in (True, False):
            v = tokens.with_dark(base, dark)
            assert v.dark == dark and themes.contrast(v.colors['text'], v.colors['bg']) >= 4.5
            assert themes.contrast(v.colors['accent'], v.colors['bg']) >= 4.5
        p = tokens.print_version(base)
        assert p.colors['bg'] == 'FFFFFF' and not p.dark and not p.shape.shadow
        assert themes.contrast(p.colors['text'], 'FFFFFF') >= 7
        assert p.hatch == (not tokens.palette_survives_greyscale(p.chart_palette)) or base.hatch
        for fmt in ('pdf', 'docx', 'pptx', 'xlsx', 'md'):
            t = tokens.legacy_theme(p if fmt == 'md' else base, fmt)
            assert themes.worst_contrast(t)[0] >= 4.5, (pid, fmt)
            assert len(t['palette']) == 6 and len(t['pdf_font']) == 4
            assert t['font'] in fonts.OFFICE_FONTS and t['heading_font'] in fonts.OFFICE_FONTS
    dark = presets.get('bold-dark')
    assert tokens.legacy_theme(dark, 'docx')['bg'] == 'FFFFFF'           # Word is read on white pages
    assert tokens.legacy_theme(dark, 'pptx')['bg'] == dark.colors['bg']
    assert tokens.legacy_theme(presets.get('editorial'), 'pdf')['pdf_font'][0] == 'Times-Roman'
    assert tokens.legacy_theme(presets.get('mono'), 'pdf')['hatch']
    greys = presets.get('minimal')
    greys.chart_palette = ['777777', '787878', '797979', '7A7A7A', '7B7B7B', '7C7C7C']
    assert tokens.print_version(greys).hatch


# ---------- live (skipped by default) ----------


@pytest.mark.skipif(os.environ.get('TG_LIVE_FONTS') != '1', reason='live font download: set TG_LIVE_FONTS=1')
async def test_live_download_from_fontsource(tmp_path, monkeypatch):
    import aiohttp
    monkeypatch.setattr(fonts, 'CACHE_DIR', tmp_path / 'fonts')
    fonts.clear_caches()
    async with aiohttp.ClientSession() as http:
        res = await fonts.ensure('Lexend', http, styles=('regular', 'bold'))
        found, offline = await fonts.search('atkinson', http)
    assert res.source == 'fontsource' and res.licence == 'OFL-1.1' and 'regular' in res.faces
    assert not offline and found[0].family.startswith('Atkinson Hyperlegible')
    fonts.clear_caches()


# ---------- a preset's open fonts in the PDF (the pastel regression) ----------

_ORDERS = {'title': 'Orders', 'subtitle': '3 rows from orders.csv', 'sections': [
    {'heading': 'Orders', 'level': 1, 'blocks': [
        {'type': 'table', 'title': 'Orders', 'columns': ['order_id', 'customer', 'amount_eur'],
         'rows': [['A-1001', 'Lindqvist AB', 1200], ['A-1002', 'Moreau SARL', 850], ['A-1003', 'Okoye Ltd', 430]]}]}]}


async def _pastel_pdf(fid: str):
    from jevrouter.create.spec import normalize
    from jevrouter.studio import agent
    spec, _ = normalize(_ORDERS, 'pdf')
    return await agent.design(spec, 'pdf', file_id=fid, preset='pastel')


async def test_pastel_pdf_embeds_its_open_fonts_when_cached(cache, design_dir):
    for fam in ('Quicksand', 'Nunito'):
        fonts._store(fam, 'OFL-1.1', 'fontsource', 'sans', {'regular': make_ttf(fam), 'bold': make_ttf(fam, 'Bold')},
                     'OFL', {})
    r = await _pastel_pdf('pastel0001ab')
    used = {(f['family'], f['embedded']) for f in r.report['fonts']}
    assert used == {('Quicksand', True), ('Nunito', True)}
    assert not any('Helvetica' in c for c in r.caveats)
    assert b'Quicksand' in r.painted_bytes and b'Nunito' in r.painted_bytes


async def test_pastel_pdf_offline_uses_the_named_substitute_not_helvetica(cache, design_dir):
    # Quicksand and Nunito are not cached and there is no network: the font manager falls back to Inter (cached), and
    # the PDF must then really use Inter, as the note says, rather than a PDF standard font
    fonts._store('Inter', 'OFL-1.1', 'fontsource', 'sans', {'regular': make_ttf('Inter'),
                                                            'bold': make_ttf('Inter', 'Bold')}, 'OFL', {})
    r = await _pastel_pdf('pastel0002ab')
    assert {(f['family'], f['embedded']) for f in r.report['fonts']} == {('Inter', True)}
    assert "Quicksand isn't installed and can't be downloaded here (offline), so this uses Inter." in r.caveats
    assert not any('Helvetica' in c for c in r.caveats)
    assert b'Inter' in r.painted_bytes


async def test_the_honest_helvetica_note_only_when_nothing_embeddable_exists(cache, design_dir):
    r = await _pastel_pdf('pastel0003ab')          # an empty cache and no system fonts: nothing to embed
    assert all(f['embedded'] is False for f in r.report['fonts'])
    assert any('so the PDF uses Helvetica' in c or 'so this uses Helvetica' in c for c in r.caveats)


def test_a_family_probed_as_missing_is_found_after_a_download(cache):
    from jevrouter.studio import layout, thumbs
    assert layout.pdf_face_ok('Test Sans') is False
    assert thumbs.font_path('Test Sans') is None
    fonts._store('Test Sans', 'OFL-1.1', 'fontsource', 'sans', {'regular': make_ttf('Test Sans')}, 'OFL', {})
    assert layout.pdf_face_ok('Test Sans') is True       # the negative probe was forgotten by clear_caches()
    assert thumbs.font_path('Test Sans', False, False).endswith('regular.ttf')
