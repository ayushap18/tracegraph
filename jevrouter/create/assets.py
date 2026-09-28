"""Licensed web images (docs/PLAN-accuracy-v2.md C5): each `figure` block becomes an embedded Wikimedia Commons image
under an allowed licence, re-encoded into the local asset cache, with a credit line.

Code looks the images up, never the model: one search per figure on the Commons API, the first candidate whose licence
is public domain, CC0, CC BY or CC BY-SA (any NC or ND term rejects it), whose type is JPEG or PNG and whose author is
named. The download goes through the same egress guard as the URL agent (every hop checked, redirects included; size
capped while streaming), then Pillow verifies the bytes, drops metadata, converts to greyscale for a black and white
file and writes a PNG named by its SHA-256 into CACHE. Renderers read images only from there (rules X3 and X6).
"""
import asyncio
import hashlib
import html
import io
import re
import time
from dataclasses import asdict, dataclass

from ..config import ROOT

ALLOWED = ('public domain', 'pd', 'cc0', 'cc by', 'cc by-sa')   # prefix match on LicenseShortName, case-insensitive; any 'nc' or 'nd' rejects
MAX_IMAGES = 8
MAX_BYTES = 3_000_000
MAX_PIXELS = 25_000_000
TYPES = ('image/jpeg', 'image/png')    # SVG, GIF, WebP, TIFF rejected
CACHE = ROOT / 'data' / 'assets'       # <sha256>.png, re-encoded

API = 'https://commons.wikimedia.org/w/api.php'
UA = {'User-Agent': 'TraceGraph/1.0 (local; contact: none)'}
SEARCH_TIMEOUT, DOWNLOAD_TIMEOUT, STAGE_TIMEOUT = 6.0, 8.0, 30.0
MAX_REDIRECTS = 3
TRIES = 3  # acceptable candidates tried per figure when a download fails
OFFLINE = "Images weren't added: web image lookup isn't available here."


class AssetError(Exception):
    """An image that can't be used (licence, type, size, a blocked address): the next candidate is tried."""


@dataclass
class Credit:
    asset: str
    title: str
    author: str
    license: str
    license_url: str | None
    source_url: str
    caption: str

    def line(self) -> str:
        return f'Image: {self.title} by {self.author}, {self.license} ({self.source_url})'

    def to_dict(self) -> dict:
        return asdict(self)


def plain_html(s) -> str:
    """Commons metadata is HTML: tags removed, entities decoded, whitespace collapsed."""
    s = re.sub(r'<[^>]+>', ' ', str(s or ''))
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()


def licence_ok(short: str) -> bool:
    s = re.sub(r'\s+', ' ', str(short or '').lower().replace('cc-by', 'cc by').replace('_', ' ')).strip()
    if not s or re.search(r'\b(?:nc|nd)\b|non-?commercial|no ?deriv', s):
        return False
    return any(s.startswith(a) for a in ALLOWED)


def _meta(ext: dict, key: str) -> str:
    v = (ext or {}).get(key)
    return plain_html(v.get('value') if isinstance(v, dict) else v)


def candidates_from(data: dict) -> list[dict]:
    """The files of one Commons search reply, in search order."""
    pages = ((data or {}).get('query') or {}).get('pages') or {}
    pages = list(pages.values()) if isinstance(pages, dict) else list(pages)
    pages.sort(key=lambda p: p.get('index', 0))
    out = []
    for p in pages:
        info = (p.get('imageinfo') or [None])[0]
        if not isinstance(info, dict):
            continue
        ext = info.get('extmetadata') or {}
        title = re.sub(r'^File:', '', str(p.get('title') or ''))
        title = re.sub(r'\.(?:jpe?g|png|gif|svg|tiff?|webp)$', '', title, flags=re.I).replace('_', ' ')
        out.append({'title': title[:160], 'url': info.get('thumburl') or info.get('url') or '',
                    'mime': str(info.get('mime') or '').lower(), 'width': int(info.get('width') or 0),
                    'height': int(info.get('height') or 0), 'license': _meta(ext, 'LicenseShortName')[:60],
                    'license_url': _meta(ext, 'LicenseUrl') or None, 'author': _meta(ext, 'Artist')[:120],
                    'source_url': str(info.get('descriptionurl') or '')})
    return out


def acceptable(c: dict) -> bool:
    return (licence_ok(c['license']) and c['mime'] in TYPES and 0 < c['width'] * c['height'] <= MAX_PIXELS
            and bool(c['author']) and bool(c['url']) and c['source_url'].startswith('https://'))


async def search_commons(http, query: str, limit: int = 5) -> list[dict]:
    import aiohttp
    params = {'action': 'query', 'generator': 'search', 'gsrnamespace': '6', 'gsrsearch': f'{query} filetype:bitmap',
              'gsrlimit': str(limit), 'prop': 'imageinfo', 'iiprop': 'url|size|mime|extmetadata', 'iiurlwidth': '1600',
              'format': 'json'}
    for attempt in range(2):
        async with http.get(API, params=params, headers=UA,
                            timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT)) as r:
            if r.status == 429 and not attempt:  # Commons asks clients to slow down: wait once, briefly
                await asyncio.sleep(retry_after(r))
                continue
            if r.status >= 400:
                raise AssetError(f'the image search answered {r.status}')
            return candidates_from(await r.json(content_type=None))
    raise AssetError('the image search is busy')


async def fetch_image(http, url: str) -> bytes:
    """The bytes of one image. Every hop passes the egress guard (http(s) only, usual web ports, public addresses);
    redirects are followed by hand and checked again; the type must be JPEG or PNG and the body at most MAX_BYTES,
    counted while it streams."""
    import aiohttp
    from yarl import URL

    from ..agents import tools
    waited = False
    for _ in range(MAX_REDIRECTS + 2):
        try:
            u = tools.check_url(str(url))
            await tools.public_addresses(u.host, u.port)
        except (tools.UrlBlocked, tools.UrlError) as e:
            raise AssetError(f'the image address was refused: {e}')
        async with http.get(u, allow_redirects=False, headers=UA,
                            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as r:
            if r.status in (301, 302, 303, 307, 308):
                loc = r.headers.get('Location')
                if not loc:
                    raise AssetError('the image redirected without saying where')
                url = str(u.join(URL(loc)))
                continue
            if r.status == 429 and not waited:
                waited = True
                await asyncio.sleep(retry_after(r))
                continue
            if r.status >= 400:
                raise AssetError(f'the image host answered {r.status}')
            ctype = (r.content_type or '').lower()
            if ctype not in TYPES:
                raise AssetError(f'it is {ctype or "an unknown type"}, not a JPEG or PNG')
            if r.content_length and r.content_length > MAX_BYTES:
                raise AssetError('the image is larger than 3 MB')
            body = bytearray()
            async for chunk in r.content.iter_chunked(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    raise AssetError('the image is larger than 3 MB')
            return bytes(body)
    raise AssetError('the image redirected too many times')


def retry_after(r) -> float:
    try:
        return min(max(float(r.headers.get('Retry-After') or 1), 0.2), 3.0)
    except (TypeError, ValueError):
        return 1.0


def reencode(data: bytes, *, mono: bool, max_side: int = 1600) -> tuple[bytes, str]:
    """Pillow: verify, strip EXIF, convert('L') when mono, PNG; returns (bytes, sha256). The pixel count is checked from
    the header before anything is decoded."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ('JPEG', 'PNG'):
                raise AssetError(f'it is {im.format or "not an image"}, not a JPEG or PNG')
            w, h = im.size
            if w * h > MAX_PIXELS or not w or not h:
                raise AssetError(f'the image is {w * h / 1e6:.0f} megapixels; the limit is {MAX_PIXELS // 1_000_000}')
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.mode in ('RGBA', 'LA', 'P'):
                im = im.convert('RGBA')
                flat = Image.new('RGB', im.size, (255, 255, 255))
                flat.paste(im, mask=im.split()[-1])
                im = flat
            im = im.convert('L' if mono else 'RGB')
            im.thumbnail((max_side, max_side))
            clean = Image.frombytes(im.mode, im.size, im.tobytes())  # pixels only: no EXIF, ICC or text chunks
    except AssetError:
        raise
    except Exception as e:
        raise AssetError(f'the image could not be read ({type(e).__name__})')
    buf = io.BytesIO()
    clean.save(buf, 'PNG', optimize=True)
    out = buf.getvalue()
    return out, hashlib.sha256(out).hexdigest()


def store(data: bytes, sha: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f'{sha}.png'
    if not path.exists():
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(path)


def guarded_session():
    """An aiohttp session whose connections use the URL agent's resolver: the address checked is the one connected to."""
    import aiohttp

    from ..agents import tools
    connector = aiohttp.TCPConnector(resolver=tools.GuardedResolver(), ssl=tools.SSL, use_dns_cache=False, limit=4)
    return aiohttp.ClientSession(connector=connector, headers=UA, trust_env=False)


def _figures(spec: dict) -> list[tuple[int, int, dict]]:
    return [(si, bi, b) for si, s in enumerate(spec.get('sections') or []) if isinstance(s, dict)
            for bi, b in enumerate(s.get('blocks') or []) if isinstance(b, dict) and b.get('type') == 'figure']


def _seed(spec: dict, seeds) -> None:
    """Research notes' `IMAGE: query | caption` ideas as figure blocks, one per section from the second on."""
    sections = [s for s in spec.get('sections') or [] if isinstance(s, dict) and isinstance(s.get('blocks'), list)]
    if not sections:
        return
    start = 1 if len(sections) > 1 else 0
    for i, (query, caption) in enumerate(list(seeds)[:MAX_IMAGES]):
        sec = sections[start + i % max(1, len(sections) - start)]
        sec['blocks'].append({'type': 'figure', 'query': str(query)[:100], 'caption': str(caption)[:200]})


def _drop(spec: dict, keep: dict) -> None:
    """Each figure replaced by its image block, or removed."""
    for si, s in enumerate(spec.get('sections') or []):
        if not isinstance(s, dict) or not isinstance(s.get('blocks'), list):
            continue
        out = []
        for bi, b in enumerate(s['blocks']):
            if isinstance(b, dict) and b.get('type') == 'figure':
                if (si, bi) in keep:
                    out.append(keep[(si, bi)])
            else:
                out.append(b)
        s['blocks'] = out


def _offline(e: BaseException) -> bool:
    import aiohttp
    return isinstance(e, (aiohttp.ClientConnectorError, aiohttp.ClientConnectionError, OSError, asyncio.TimeoutError))


async def resolve_figures(spec: dict, http, *, mono: bool,
                          seeds: list[tuple[str, str]] = ()) -> tuple[dict, list[Credit], list[str]]:
    """(the spec with figures resolved to images, their credits, caveats). Without `http` every figure is removed."""
    import copy
    spec = copy.deepcopy(spec)
    if not _figures(spec) and seeds:
        _seed(spec, seeds)
    figures = _figures(spec)
    if not figures:
        return spec, [], []
    if http is None:
        _drop(spec, {})
        return spec, [], [OFFLINE]
    import aiohttp
    own = isinstance(http, aiohttp.ClientSession)
    session = guarded_session() if own else http
    credits, caveats, keep, used = [], [], {}, set()
    if len(figures) > MAX_IMAGES:
        caveats.append(f'Only {MAX_IMAGES} images are added to one file; {len(figures) - MAX_IMAGES} more were left out.')
        figures = figures[:MAX_IMAGES]
    deadline = time.monotonic() + STAGE_TIMEOUT
    try:
        for n, (si, bi, fig) in enumerate(figures):
            caption = str(fig.get('caption') or fig.get('query') or '').strip()
            query = str(fig.get('query') or caption).strip()
            left = deadline - time.monotonic()
            if left <= 0:
                caveats += [f'the image lookup ran out of time for "{f.get("caption") or f.get("query")}"'
                            for _, _, f in figures[n:]]
                break
            try:
                found = await asyncio.wait_for(_one(session, query, mono, used), left)
            except Exception as e:
                if time.monotonic() >= deadline - 0.05:  # the whole stage's time is up
                    caveats += [f'the image lookup ran out of time for "{f.get("caption") or f.get("query")}"'
                                for _, _, f in figures[n:]]
                    break
                if _offline(e) and n == 0:  # the first lookup couldn't reach Commons at all
                    caveats.append(OFFLINE)
                    break
                found = None
            if found is None:
                caveats.append(f'no suitably licensed image found for "{caption}"')
                continue
            cand, sha = found
            used.add(sha)
            credit = Credit(sha, cand['title'], cand['author'], cand['license'], cand['license_url'],
                            cand['source_url'], caption)
            credits.append(credit)
            keep[(si, bi)] = {'type': 'image', 'asset': sha, 'caption': caption, 'credit': credit.line()}
    finally:
        if own:
            await session.close()
    _drop(spec, keep)
    if credits:
        spec.setdefault('sections', []).append({'heading': 'Image credits', 'level': 1, 'notes': '', 'blocks': [
            {'type': 'bullets', 'ordered': False, 'items': [c.line() for c in credits]}]})
    return spec, credits, caveats


async def _one(http, query: str, mono: bool, used: set) -> tuple[dict, str] | None:
    """The first acceptable candidate for a query that downloads and re-encodes, as (candidate, sha256)."""
    tried = 0
    for c in await search_commons(http, query):
        if not acceptable(c):
            continue
        tried += 1
        try:
            data, sha = reencode(await fetch_image(http, c['url']), mono=mono)
        except AssetError:
            if tried >= TRIES:
                return None
            continue
        if sha in used:
            continue
        store(data, sha)
        return c, sha
    return None
