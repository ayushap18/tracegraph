"""Licensed web images (docs/PLAN-accuracy-v2.md C5), with a fake aiohttp session (no network): the licence and type
rules, the egress guard on every hop, size caps while streaming, re-encoding without metadata, greyscale for black and
white files, credits in the file, and honest caveats when nothing suitable is found."""
import asyncio
import io
import struct
import zlib

import pytest
from PIL import Image

from jevrouter.agents import tools
from jevrouter.create import SpecError, assets, normalize, render, strip_internal, verify
from jevrouter.create.brief import parse_brief

PUBLIC_IP = '198.35.26.112'


def jpeg(size=(64, 48), color=(200, 30, 30), exif=True) -> bytes:
    im = Image.new('RGB', size, color)
    buf = io.BytesIO()
    if exif:
        e = Image.Exif()
        e[0x010F] = 'SecretCam'
        e[0x0132] = '2026:01:01 10:00:00'
        im.save(buf, 'JPEG', exif=e)
    else:
        im.save(buf, 'JPEG')
    return buf.getvalue()


def png_claiming(width: int, height: int) -> bytes:
    """A PNG whose header claims width x height, with almost no pixel data behind it."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(b'\x00' * 10)) + chunk(b'IEND', b'')


class Content:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, n):
        for i in range(0, len(self.body), n):
            await asyncio.sleep(0)
            yield self.body[i:i + n]


class Resp:
    def __init__(self, status=200, body=b'', ctype='image/jpeg', headers=None, data=None, length=None):
        self.status, self.content_type, self.headers = status, ctype, headers or {}
        self.content, self.content_length, self.data = Content(body), length, data

    async def json(self, content_type=None):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def candidate(name, license='CC BY-SA 4.0', mime='image/jpeg', artist='<a href="https://x">Jane Doe</a>', size=(800, 600),
              index=1) -> dict:
    url = f'https://upload.wikimedia.org/{name}'
    return {'index': index, 'title': f'File:{name}', 'imageinfo': [{
        'thumburl': url, 'url': url, 'mime': mime, 'width': size[0], 'height': size[1],
        'descriptionurl': f'https://commons.wikimedia.org/wiki/File:{name}',
        'extmetadata': {'LicenseShortName': {'value': license}, 'Artist': {'value': artist},
                        'LicenseUrl': {'value': 'https://creativecommons.org/licenses/by-sa/4.0'}}}]}


class FakeHTTP:
    """search: query -> [candidate pages]; files: url -> Resp (or an exception to raise)."""

    def __init__(self, search=None, files=None, delay=0.0, down=False):
        self.search, self.files, self.delay, self.down = search or {}, files or {}, delay, down
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((str(url), params))
        if self.down:
            raise OSError('network is unreachable')
        if params is not None:
            query = params['gsrsearch'].removesuffix(' filetype:bitmap')
            pages = {str(i): p for i, p in enumerate(self.search.get(query, []))}
            return self._later(Resp(ctype='application/json', data={'query': {'pages': pages}} if pages else {}))
        item = self.files.get(str(url), Resp(404))
        if isinstance(item, Exception):
            raise item
        return self._later(item)

    def _later(self, resp):
        delay = self.delay

        class Ctx:
            async def __aenter__(self):
                await asyncio.sleep(delay)
                return resp

            async def __aexit__(self, *exc):
                return False
        return Ctx()


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """Images go to a temporary asset cache, and every host resolves to a public address (no DNS)."""
    monkeypatch.setattr(assets, 'CACHE', tmp_path / 'assets')

    async def lookup(host, port):
        return [PUBLIC_IP]
    monkeypatch.setattr(tools, 'lookup', lookup)
    return tmp_path / 'assets'


def figure_spec(*captions, theme='clean') -> dict:
    return {'title': 'Computing history', 'theme': theme, 'sections': [
        {'heading': 'Early machines', 'level': 1, 'blocks': [{'type': 'paragraph', 'text': 'The first computers.'},
                                                             *[{'type': 'figure', 'query': c, 'caption': c}
                                                               for c in captions]]}]}


def blocks(spec) -> list[dict]:
    return [b for s in spec['sections'] for b in s['blocks']]


# ---------- licences and candidates ----------

@pytest.mark.parametrize('short,ok', [
    ('Public domain', True), ('PD-US', True), ('CC0', True), ('CC BY 4.0', True), ('CC BY-SA 3.0', True),
    ('cc-by-sa-4.0', True), ('CC BY-NC 4.0', False), ('CC BY-NC-SA 2.0', False), ('CC BY-ND 4.0', False),
    ('GFDL', False), ('', False), ('Fair use', False)])
def test_licences(short, ok):
    assert assets.licence_ok(short) is ok


def test_candidates_need_a_licence_a_type_an_author_and_a_sane_size():
    pages = {'query': {'pages': {'2': candidate('b.png', index=2), '1': candidate('a.jpg', index=1)}}}
    cs = assets.candidates_from(pages)
    assert [c['title'] for c in cs] == ['a', 'b'] and cs[0]['author'] == 'Jane Doe' and all(map(assets.acceptable, cs))
    bad = [candidate('c.svg', mime='image/svg+xml'), candidate('d.gif', mime='image/gif'),
           candidate('e.jpg', license='CC BY-NC 4.0'), candidate('f.jpg', artist=''),
           candidate('g.jpg', size=(6000, 5000))]
    assert not any(assets.acceptable(c) for c in assets.candidates_from({'query': {'pages': bad}}))


# ---------- the download ----------

async def test_a_redirect_to_a_private_address_is_blocked():
    http = FakeHTTP(files={'https://upload.wikimedia.org/a.jpg': Resp(302, headers={'Location': 'http://10.0.0.1/a.jpg'})})
    with pytest.raises(assets.AssetError, match='refused'):
        await assets.fetch_image(http, 'https://upload.wikimedia.org/a.jpg')
    assert [u for u, _ in http.calls] == ['https://upload.wikimedia.org/a.jpg']  # 10.0.0.1 was never asked
    for bad in ('file:///etc/passwd', 'http://127.0.0.1/x.png', 'https://user:pw@upload.wikimedia.org/x.png',
                'https://upload.wikimedia.org:22/x.png'):
        with pytest.raises(assets.AssetError):
            await assets.fetch_image(http, bad)


async def test_types_and_sizes_are_capped_while_streaming():
    url = 'https://upload.wikimedia.org/x'
    for resp, why in ((Resp(body=b'<html>', ctype='text/html'), 'text/html'),
                      (Resp(body=b'<svg/>', ctype='image/svg+xml'), 'svg'),
                      (Resp(body=b'x' * 4_000_000), 'larger than 3 MB'),
                      (Resp(body=b'x', length=4_000_000), 'larger than 3 MB')):
        with pytest.raises(assets.AssetError, match=why):
            await assets.fetch_image(FakeHTTP(files={url: resp}), url)
    ok = await assets.fetch_image(FakeHTTP(files={url: Resp(body=jpeg())}), url)
    assert ok[:2] == b'\xff\xd8'


def test_reencode_checks_the_header_before_decoding_and_drops_metadata():
    with pytest.raises(assets.AssetError, match='30 megapixels'):
        assets.reencode(png_claiming(6000, 5000), mono=False)
    with pytest.raises(assets.AssetError):
        assets.reencode(b'<svg xmlns="http://www.w3.org/2000/svg"/>', mono=False)
    gif = io.BytesIO()
    Image.new('RGB', (4, 4)).save(gif, 'GIF')
    with pytest.raises(assets.AssetError, match='GIF'):
        assets.reencode(gif.getvalue(), mono=False)
    src = jpeg()
    assert Image.open(io.BytesIO(src)).getexif()
    data, sha = assets.reencode(src, mono=False)
    out = Image.open(io.BytesIO(data))
    assert out.format == 'PNG' and out.mode == 'RGB' and not out.getexif() and 'exif' not in out.info
    assert len(sha) == 64 and b'SecretCam' not in data
    grey, _ = assets.reencode(src, mono=True)
    assert Image.open(io.BytesIO(grey)).mode == 'L'
    big, _ = assets.reencode(jpeg(size=(3000, 1000), exif=False), mono=False)
    assert max(Image.open(io.BytesIO(big)).size) == 1600


# ---------- figures in a file ----------

async def test_figures_become_credited_images(cache):
    http = FakeHTTP(search={
        'ENIAC': [candidate('eniac-nc.jpg', license='CC BY-NC 4.0', index=1), candidate('ENIAC_Penn1.jpg', index=2)],
        'Alan Turing': [candidate('Turing.jpg', license='Public domain', artist='Unknown photographer')]},
        files={'https://upload.wikimedia.org/ENIAC_Penn1.jpg': Resp(body=jpeg()),
               'https://upload.wikimedia.org/Turing.jpg': Resp(body=jpeg(color=(10, 90, 200)))})
    spec, credits, caveats = await assets.resolve_figures(figure_spec('ENIAC', 'Alan Turing'), http, mono=True)
    assert caveats == [] and len(credits) == 2
    assert not any(u.endswith('eniac-nc.jpg') for u, _ in http.calls)  # the NC file was never downloaded
    c = credits[0]
    assert (c.title, c.author, c.license) == ('ENIAC Penn1', 'Jane Doe', 'CC BY-SA 4.0')
    assert c.line() == ('Image: ENIAC Penn1 by Jane Doe, CC BY-SA 4.0 '
                        '(https://commons.wikimedia.org/wiki/File:ENIAC_Penn1.jpg)')
    images = [b for b in blocks(spec) if b['type'] == 'image']
    assert [b['credit'] for b in images] == [x.line() for x in credits] and [b['caption'] for b in images] == ['ENIAC', 'Alan Turing']
    assert not any(b['type'] == 'figure' for b in blocks(spec))
    assert spec['sections'][-1]['heading'] == 'Image credits' and spec['sections'][-1]['blocks'][0]['items'] == \
        [x.line() for x in credits]
    assert all(Image.open(cache / f'{x.asset}.png').mode == 'L' for x in credits)

    # the PDF embeds exactly those images, greyscale, with the credits in its text
    from pypdf import PdfReader

    from jevrouter.create.rules import grey_scan, pdf_scan
    spec['theme'] = 'mono'
    data = render(spec, 'pdf')
    scan = pdf_scan(data)
    assert scan['images'] == ['/DeviceGray', '/DeviceGray'] and grey_scan(data)[0]
    text = ' '.join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert 'Image credits' in text and 'Jane Doe' in text and 'Unknown photographer' in text
    results = {r.id: r for r in verify(spec, 'pdf', data, brief=parse_brief('a black and white pdf with images'))}
    assert results['X6'].ok and '2 images from the asset cache' in results['X6'].note
    assert results['V6'].ok and results['V8'].ok
    for fmt in ('docx', 'pptx'):
        data = render(spec, fmt)
        results = {r.id: r for r in verify(spec, fmt, data, brief=parse_brief('with images'))}
        assert results['V6'].ok and results['V6'].note == '2 images embedded' and results['X6'].ok, fmt
    md = render(spec, 'md').decode()
    assert 'Image: ENIAC Penn1 by Jane Doe' in md and '![' not in md and 'upload.wikimedia.org' not in md


async def test_no_hits_no_http_and_offline_are_honest():
    spec, credits, caveats = await assets.resolve_figures(figure_spec('Nothing'), FakeHTTP(), mono=False)
    assert credits == [] and caveats == ['no suitably licensed image found for "Nothing"']
    assert [b['type'] for b in blocks(spec)] == ['paragraph']  # no broken block left behind
    assert render(spec, 'pdf')[:4] == b'%PDF'
    spec, _, caveats = await assets.resolve_figures(figure_spec('a', 'b'), None, mono=False)
    assert caveats == [assets.OFFLINE] and [b['type'] for b in blocks(spec)] == ['paragraph']
    spec, _, caveats = await assets.resolve_figures(figure_spec('a', 'b'), FakeHTTP(down=True), mono=False)
    assert caveats == [assets.OFFLINE] and [b['type'] for b in blocks(spec)] == ['paragraph']


async def test_seeds_are_used_when_the_writer_gave_no_figures():
    http = FakeHTTP(search={'Deep Blue chess computer': [candidate('Deep_Blue.jpg')]},
                    files={'https://upload.wikimedia.org/Deep_Blue.jpg': Resp(body=jpeg())})
    plain = {'title': 'T', 'sections': [{'heading': 'A', 'blocks': [{'type': 'paragraph', 'text': 'x'}]},
                                        {'heading': 'B', 'blocks': [{'type': 'paragraph', 'text': 'y'}]}]}
    spec, credits, _ = await assets.resolve_figures(plain, http, mono=False,
                                                    seeds=[('Deep Blue chess computer', 'Deep Blue in 1997')])
    assert len(credits) == 1 and [b['type'] for b in spec['sections'][1]['blocks']] == ['paragraph', 'image']


async def test_the_asset_stage_has_a_deadline(monkeypatch):
    monkeypatch.setattr(assets, 'STAGE_TIMEOUT', 0.05)
    spec, credits, caveats = await assets.resolve_figures(figure_spec('slow one', 'slow two'), FakeHTTP(delay=0.2),
                                                          mono=False)
    assert credits == [] and caveats == ['the image lookup ran out of time for "slow one"',
                                         'the image lookup ran out of time for "slow two"']
    assert [b['type'] for b in blocks(spec)] == ['paragraph']


async def test_too_many_figures_are_capped():
    spec, _, caveats = await assets.resolve_figures(figure_spec(*[f'f{i}' for i in range(10)]), FakeHTTP(), mono=False)
    assert caveats[0] == 'Only 8 images are added to one file; 2 more were left out.' and len(caveats) == 9


# ---------- X6: images only from the cache, always credited ----------

async def test_x6_image_blocks(cache):
    data, sha = assets.reencode(jpeg(), mono=False)
    assets.store(data, sha)
    ok = {'type': 'image', 'asset': sha, 'caption': 'c', 'credit': 'Image: x by y, CC0 (https://commons.wikimedia.org/x)'}
    spec = {'title': 'T', 'sections': [{'heading': 'A', 'blocks': [{'type': 'paragraph', 'text': 'x'}, ok]}]}
    assert blocks(normalize(spec, 'pdf')[0])[1] == ok
    missing = {**ok, 'asset': 'f' * 64}
    spec['sections'][0]['blocks'][1] = missing
    assert [b['type'] for b in blocks(normalize(spec, 'pdf')[0])] == ['paragraph']  # not in the cache: left out
    spec['sections'][0]['blocks'][1] = {**ok, 'credit': ''}
    with pytest.raises(SpecError) as e:
        normalize(spec, 'pdf')
    assert e.value.rule_id == 'X6'
    # the model can never write an image block (its asset id would name a local file)
    written = {'title': 'T', 'font': 'x', 'sections': [{'heading': 'A', 'blocks': [ok, {'type': 'paragraph', 'text': 'x'}]}]}
    assert strip_internal(written) == {'title': 'T', 'sections': [{'heading': 'A', 'blocks': [{'type': 'paragraph',
                                                                                               'text': 'x'}]}]}
    # and verify refuses a file whose image vanished from the cache after rendering
    spec['sections'][0]['blocks'][1] = ok
    pdf = render(spec, 'pdf')
    (cache / f'{sha}.png').write_bytes(b'not a png')
    with pytest.raises(SpecError) as e:
        verify(spec, 'pdf', pdf)
    assert e.value.rule_id == 'X6'


async def test_a_colour_image_turns_grey_in_a_black_and_white_file(cache):
    from jevrouter.create.rules import grey_scan
    data, sha = assets.reencode(jpeg(color=(200, 30, 30)), mono=False)
    assets.store(data, sha)
    spec = {'title': 'T', 'theme': 'mono', 'sections': [{'heading': 'A', 'blocks': [
        {'type': 'image', 'asset': sha, 'caption': 'red', 'credit': 'Image: x by y, CC0 (https://commons.wikimedia.org/x)'}]}]}
    assert grey_scan(render(spec, 'pdf')) == (True, 'only greys drawn; 1 greyscale image')
    assert not grey_scan(render({**spec, 'theme': 'clean'}, 'pdf'))[0]
