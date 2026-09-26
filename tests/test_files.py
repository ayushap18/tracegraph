"""Uploaded files: extraction, passage search, table stats, and the document/data agents with and without an engine."""
import json

import pytest

from jevrouter.files import FileError, chunks, extract, file_agents, search, table_stats, to_table
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeJev, ScriptEngine
from tests.test_pipeline import run


def tiny_pdf(text: str) -> bytes:
    """A one-page PDF with one line of Helvetica text, built by hand (xref offsets computed) so pypdf can extract it."""
    stream = f'BT /F1 12 Tf 72 712 Td ({text}) Tj ET'.encode()
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
            b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % i + o + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % x for x in offsets)
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return bytes(out)


CSV = 'region,month,units,revenue\nNorth,Jan,10,"1,000"\nSouth,Jan,30,2500\nNorth,Feb,20,1500.5\nSouth,Feb,n/a,3000\n'
NOTES = ('# Project Falcon\n\nThe launch date for Project Falcon is 14 March. The budget is 2 million euros.\n\n'
         'The team lead is Priya Nair, based in Bangalore.\n\n' + 'Filler sentence about nothing in particular. ' * 40)


def test_extract_kinds():
    meta, text = extract('notes.md', NOTES.encode())
    assert meta['kind'] == 'text' and meta['chars'] == len(NOTES) and 'rows' not in meta and len(meta['id']) == 12
    meta, _ = extract('sales.csv', CSV.encode())
    assert meta['kind'] == 'csv' and meta['rows'] == 4 and meta['columns'] == ['region', 'month', 'units', 'revenue']
    meta, text = extract('Report.PDF', tiny_pdf('Quarterly revenue grew 12 percent'))
    assert meta['kind'] == 'pdf' and 'Quarterly revenue grew 12 percent' in text
    meta, _ = extract('people.json', json.dumps({'people': [{'name': 'A', 'age': 30}, {'name': 'B', 'age': 40}]}).encode())
    assert meta['kind'] == 'json' and meta['rows'] == 2 and meta['columns'] == ['name', 'age']
    for name, raw in (('a.exe', b'x'), ('noext', b'x'), ('bad.json', b'{nope'), ('broken.pdf', b'%PDF-1.4 garbage')):
        with pytest.raises(FileError):
            extract(name, raw)


def test_chunks_and_search():
    parts = chunks(NOTES)
    assert all(len(c) <= 800 for c in parts) and len(parts) >= 2
    assert chunks('x' * 2000) == ['x' * 800, 'x' * 800, 'x' * 400]
    hits = search([('notes.md', NOTES), ('other.txt', 'Bananas are yellow.')], 'Who is the team lead for Falcon?')
    assert hits[0][1] == 'notes.md' and 'Priya Nair' in hits[0][2]
    assert search([('a', 'alpha beta')], 'zzz') == []


def test_table_stats():
    cols, rows = to_table('csv', CSV)
    s = table_stats('sales.csv', cols, rows)
    assert s.startswith('sales.csv: 4 rows × 4 columns (region, month, units, revenue)')
    assert '- units: min 10, mean 20, max 30, sum 60 (3 values)' in s  # "n/a" is skipped, not fatal
    assert '- revenue: min 1,000, mean 2,000.12, max 3,000, sum 8,000.50 (4 values)' in s
    assert '- region: 2 distinct' in s
    assert to_table('json', '{"a": 1}') is None and to_table('text', 'x') is None


def files_for(*items):
    metas, texts = [], {}
    for name, raw in items:
        m, t = extract(name, raw)
        metas.append(m)
        texts[m['id']] = t
    return metas, texts


async def test_document_and_data_agents_keyless():
    metas, texts = files_for(('notes.md', NOTES.encode()), ('sales.csv', CSV.encode()))
    ags = file_agents(metas, texts)
    shown = []
    r = await ags['document']('When is the Falcon launch date?', shown.append)
    assert r.ok and r.answer.startswith('**notes.md**: ') and '14 March' in r.answer and r.engine == 'keyless'
    assert shown == [r.answer]
    r = await ags['document']('summarize', lambda t: None)  # no overlap: falls back to each file's opening
    assert r.ok and 'start of each file' in r.answer
    r = await ags['data']('total revenue?', lambda t: None)
    assert r.ok and 'sum 8,000.50' in r.answer and r.source == 'sales.csv'
    only_text = file_agents(*files_for(('notes.md', NOTES.encode())))
    assert not (await only_text['data']('average?', lambda t: None)).ok


async def test_document_and_data_agents_with_engine():
    metas, texts = files_for(('notes.md', NOTES.encode()), ('sales.csv', CSV.encode()))
    engine = ScriptEngine()
    ags = file_agents(metas, texts, engine)
    r = await ags['document']('Who is the team lead?', lambda t: None)
    call = engine.calls[-1]
    assert r.engine == 'claude-code' and '[notes.md] passage 1:' in call['prompt'] and 'Priya Nair' in call['prompt']
    assert 'citing the file name' in call['system']
    await ags['data']('Which region sold more?', lambda t: None)
    p = engine.calls[-1]['prompt']
    assert 'Computed statistics:\nsales.csv: 4 rows' in p and 'sales.csv (first 4 rows):\nregion,month,units,revenue\nNorth,Jan,10,"1,000"' in p


async def test_run_with_files_offers_file_agents():
    store = Store()
    meta, text = extract('sales.csv', CSV.encode())
    store.add_file(meta, CSV.encode(), text)
    jev = FakeJev(route_for=lambda t: ('data', 0.9) if 'total' in t else ('chat', 0.9))
    router = Router(jev, store=store)
    events = []
    router.bus.taps.append(events.append)
    await router.handle('total units?', 'you', files=[meta['id']])
    assert {'document', 'data'} <= set(jev.criteria[-1])
    assert jev.calls[-1][0] == 'total units?\n\n(Attached files: sales.csv)'  # Jev is told what's attached
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'data' and 'sum 60' in a['answer'] and events[0]['files'] == [meta['id']]
    await run(router, 'hey friend')
    assert 'data' not in jev.criteria[-1]  # without attachments the file agents aren't offered
    assert store.list_files()[0]['rows'] == 4 and store.delete_file(meta['id']) and not store.list_files()
