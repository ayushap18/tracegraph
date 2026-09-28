"""The DocSpec: schema, normalize (S1-S7, L1-L3), zero-token specs from Markdown and tables, format detection, names."""
import copy
import json

import pytest

from jevrouter.create import DOCSPEC_SCHEMA, FORMATS, SpecError, detect_format, file_name, from_markdown, from_table
from jevrouter.create import normalize
from jevrouter.create.spec import chart_summary, number, plain, runs
from jevrouter.files import to_table


def spec_with(*blocks, heading='Section', **top):
    return {'title': 'Test file', **top, 'sections': [{'heading': heading, 'blocks': list(blocks)}]}


def para(text):
    return {'type': 'paragraph', 'text': text}


def blocked(spec, fmt='pdf') -> str:
    with pytest.raises(SpecError) as e:
        normalize(spec, fmt)
    return e.value.rule_id


def result(results, rid):
    return next(r for r in results if r.id == rid)


# ---------- schema ----------

def test_schema_is_strict_small_and_ref_free():
    text = json.dumps(DOCSPEC_SCHEMA)
    # diagrams, figures and page breaks (docs/PLAN-accuracy-v2.md C4) made it a little longer than the first 4,000
    assert '$ref' not in text and '$defs' not in text and len(text) < 5000

    def walk(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node['additionalProperties'] is False
                assert sorted(node['required']) == sorted(node['properties'])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(DOCSPEC_SCHEMA)
    kinds = [b['properties']['type']['enum'][0] for b in
             DOCSPEC_SCHEMA['properties']['sections']['items']['properties']['blocks']['items']['anyOf']]
    # the model writes every block type but `image`, which only code makes from a resolved figure
    assert kinds == ['paragraph', 'bullets', 'table', 'chart', 'quote', 'code', 'timeline', 'tree', 'flow', 'figure',
                     'page_break']


# ---------- block rules ----------

@pytest.mark.parametrize('spec,rid', [
    ('not a spec', 'S1'),
    ({'title': 'x'}, 'S1'),
    ({'title': 'x', 'sections': 'nope'}, 'S1'),
    ({'title': 'x', 'sections': ['nope']}, 'S1'),
    (spec_with({'type': 'image', 'text': 'x'}), 'S1'),
    (spec_with({'type': 'table', 'columns': ['a']}), 'S1'),
    (spec_with({'type': 'chart', 'kind': 'radar', 'labels': ['a'], 'series': [{'name': 'n', 'values': [1]}]}), 'S1'),
    (spec_with({'type': 'bullets', 'items': 'one'}), 'S1'),
    ({'title': 'x', 'sections': []}, 'S2'),
    (spec_with(para(''), {'type': 'bullets', 'items': ['', ' ']}), 'S2'),
    ({'title': 'x', 'sections': [{'heading': 'Only a heading', 'blocks': []}]}, 'S2'),
    (spec_with({'type': 'paragraph', 'text': 'x', 'url': 'https://evil.example/a.png'}), 'S7'),
    ({'title': 'x', 'sections': [{'heading': 'h', 'blocks': [para('x')], 'include': '/etc/passwd'}]}, 'S7'),
    ({'title': 'x', 'sections': [{'heading': f'h{i}', 'blocks': [para('x')]} for i in range(41)]}, 'L1'),
    (spec_with(*[para('x')] * 31), 'L1'),
    (spec_with(para('word ' * 41000)), 'L1'),
])
def test_block_rules_raise_with_their_id(spec, rid):
    assert blocked(spec) == rid


def test_unknown_format_is_x1():
    assert blocked(spec_with(para('x')), 'docm') == 'X1'
    assert blocked(spec_with(para('x')), 'html') == 'X1'


def test_spec_error_carries_rule_and_message():
    with pytest.raises(SpecError) as e:
        normalize({'title': 'x', 'sections': []}, 'md')
    assert e.value.rule_id == 'S2' and 'section' in e.value.message and str(e.value).startswith('S2:')


def test_l1_budget_leaves_table_rows_to_l2():
    rows = [[f'row {i}', i, i * 2.5, 'some longer text here to make the rows take room'] for i in range(4000)]
    spec = spec_with({'type': 'table', 'columns': ['a', 'b', 'c', 'd'], 'rows': rows})
    assert len(json.dumps(spec)) > 200_000
    out, res = normalize(spec, 'xlsx')
    assert len(out['sections'][0]['blocks'][0]['rows']) == 2000 and result(res, 'L1').ok  # L2 cut it, L1 passed


# ---------- fix rules ----------

def test_results_cover_content_and_size_rules_and_input_is_untouched():
    spec = spec_with(para('plain'))
    before = copy.deepcopy(spec)
    out, res = normalize(spec, 'pdf')
    assert [r.id for r in res][:10] == ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'L1', 'L2', 'L3']
    assert all(r.ok for r in res) and spec == before
    assert out['format'] == 'pdf' and out['theme'] == 'clean' and out['paper'] == 'a4'


def test_s3_title_from_first_heading_or_text_and_shortened():
    out, res = normalize({'sections': [{'heading': 'Battery basics', 'blocks': [para('x')]}]}, 'md')
    assert out['title'] == 'Battery basics' and not result(res, 'S3').ok
    out, _ = normalize({'title': '', 'sections': [{'heading': '', 'blocks': [para('Lithium ion cells store energy '
                                                                                  'in many ways today')]}]}, 'md')
    assert out['title'] == 'Lithium ion cells store energy in many ways'
    out, res = normalize(spec_with(para('x'), title='word ' * 60), 'md')
    assert 1 <= len(out['title']) <= 120 and not out['title'].endswith(' ') and not result(res, 'S3').ok


def test_s4_plain_text_keeps_bold_and_italic_only():
    assert plain('<p>Hello <b>big</b> <em>world</em></p><script>alert(1)</script>') == 'Hello **big** *world*'
    assert plain('See [the docs](https://example.com/x) and [local](file:///etc/passwd)') == \
        'See the docs (https://example.com/x) and local'
    assert plain('![chart](https://evil.example/c.png) done') == 'chart done'
    assert plain('## Heading\n> quoted `code` ~~old~~ __strong__ _soft_') == 'Heading\nquoted code old **strong** *soft*'
    assert plain('**bold** and *it*', emphasis=False) == 'bold and it'
    assert plain('5 * 3 = 15 and a < b > c') == '5 * 3 = 15 and a < b > c'
    assert plain('Tom &amp; Jerry <br> next') == 'Tom & Jerry\nnext'
    assert plain('bad\x00chars\x1b here') == 'badchars here'
    assert runs('a **b** *c* d') == [('a ', False, False), ('b', True, False), (' ', False, False), ('c', False, True),
                                     (' d', False, False)]


def test_s4_markdown_list_in_a_paragraph_becomes_bullets():
    out, res = normalize(spec_with(para('Intro line\n\n- one\n- two\n\n1. first\n2. second')), 'docx')
    blocks = out['sections'][0]['blocks']
    assert blocks[0] == para('Intro line')
    assert blocks[1] == {'type': 'bullets', 'items': ['one', 'two'], 'ordered': False}
    assert blocks[2] == {'type': 'bullets', 'items': ['first', 'second'], 'ordered': True}
    assert not result(res, 'S4').ok


def test_s5_numbers():
    assert [number(x) for x in ('1,200', '-3.5', '42', '007', '2024-01-05', '1e5', '12%', '', 'n/a')] == \
        [1200, -3.5, 42, None, None, None, None, None, None]
    assert [number(x, loose=True) for x in ('$1,200', '12%', '(300)', '-€5', '1 000')] == [1200, 12, -300, -5, 1000]
    assert number('1234567890123456') is None and number(True) is None and number(float('nan')) is None
    spec = spec_with({'type': 'table', 'columns': ['a', 'b'], 'rows': [['1,200', '007'], [3, None]]},
                     {'type': 'chart', 'kind': 'bar', 'title': 't', 'labels': ['x', 'y', 'z'],
                      'series': [{'name': 's', 'values': ['$5', 'n/a', 7]}]})
    out, res = normalize(spec, 'xlsx')
    table, chart = out['sections'][0]['blocks']
    assert table['rows'] == [[1200, '007'], [3, None]]
    assert chart['series'][0]['values'] == [5, None, 7] and not result(res, 'S5').ok


def test_s5_pie_drops_non_positive_slices_and_chart_without_numbers_goes():
    spec = spec_with({'type': 'chart', 'kind': 'donut', 'title': 'p', 'labels': ['a', 'b', 'c'],
                      'series': [{'name': 's', 'values': [3, 0, -1]}]},
                     {'type': 'chart', 'kind': 'bar', 'title': 'none', 'labels': ['a'],
                      'series': [{'name': 's', 'values': ['x']}]}, para('keep'))
    out, _ = normalize(spec, 'pdf')
    blocks = out['sections'][0]['blocks']
    assert blocks[0]['kind'] == 'pie' and blocks[0]['labels'] == ['a'] and len(blocks) == 2


def test_s6_rows_padded_and_trimmed_and_missing_columns_named():
    spec = spec_with({'type': 'table', 'columns': ['a', 'b', 'c'], 'rows': [['1'], ['1', '2', '3', '4'], 'x']},
                     {'type': 'table', 'columns': [], 'rows': [['p', 'q']]})
    out, res = normalize(spec, 'md')
    t1, t2 = out['sections'][0]['blocks']
    assert t1['rows'] == [[1, None, None], [1, 2, 3], ['x', None, None]]
    assert t2['columns'] == ['Column 1', 'Column 2'] and not result(res, 'S6').ok


def test_a3_chart_without_title_gets_one():
    out, res = normalize(spec_with({'type': 'chart', 'kind': 'bar', 'labels': ['a'],
                                    'series': [{'name': 'Revenue', 'values': [1]}]}), 'pptx')
    assert out['sections'][0]['blocks'][0]['title'] == 'Revenue' and not result(res, 'A3').ok


def test_levels_never_skip():
    spec = {'title': 't', 'sections': [{'heading': 'a', 'level': 3, 'blocks': [para('x')]},
                                       {'heading': 'b', 'level': 3, 'blocks': [para('x')]},
                                       {'heading': 'c', 'level': 1, 'blocks': [para('x')]},
                                       {'heading': 'd', 'level': 9, 'blocks': [para('x')]}]}
    out, res = normalize(spec, 'md')
    assert [s['level'] for s in out['sections']] == [1, 2, 1, 2] and not result(res, 'F5').ok


def test_l2_long_tables_cut_with_a_note_per_format():
    rows = [[i, f'r{i}'] for i in range(2500)]
    for fmt, cap in (('pdf', 200), ('docx', 200), ('md', 200), ('xlsx', 2000)):
        out, res = normalize(spec_with({'type': 'table', 'columns': ['n', 'name'], 'rows': rows}), fmt)
        table, note = out['sections'][0]['blocks']
        assert len(table['rows']) == cap and 'first' in note['text'] and '2,500' in note['text']
        assert not result(res, 'L2').ok
    wide = spec_with({'type': 'table', 'columns': [f'c{i}' for i in range(40)], 'rows': [list(range(40))]})
    out, _ = normalize(wide, 'xlsx')
    assert len(out['sections'][0]['blocks'][0]['columns']) == 30 and len(out['sections'][0]['blocks'][0]['rows'][0]) == 30


def test_l2_slides_split_tables_every_12_rows():
    rows = [[i, i * 2] for i in range(30)]
    out, res = normalize(spec_with({'type': 'table', 'columns': ['a', 'b'], 'rows': rows}, heading='Data'), 'pptx')
    slides = out['sections']
    assert [len(s['blocks'][0]['rows']) for s in slides] == [12, 12, 6]
    assert [s['heading'] for s in slides] == ['Data', 'Data (cont.)', 'Data (cont.)'] and not result(res, 'L2').ok
    wide = spec_with({'type': 'table', 'columns': [f'c{i}' for i in range(10)], 'rows': [list(range(10))]})
    out, _ = normalize(wide, 'pptx')
    assert len(out['sections'][0]['blocks'][0]['columns']) == 8 and 'c8, c9' in out['sections'][0]['notes']


def test_l3_bullets_and_long_text_move_to_notes():
    items = [f'point {i}' for i in range(10)] + ['word ' * 25]
    out, res = normalize(spec_with({'type': 'bullets', 'items': items}, heading='Many'), 'pptx')
    slide = out['sections'][0]
    shown = slide['blocks'][0]['items']
    assert len(shown) == 6 and shown == items[:6]
    assert 'point 9' in slide['notes'] and 'word word' in slide['notes'] and not result(res, 'L3').ok
    long = ' '.join(f'w{i}' for i in range(30))
    out, _ = normalize(spec_with({'type': 'bullets', 'items': [long]}), 'pptx')
    kept = out['sections'][0]['blocks'][0]['items'][0]
    assert len(kept.split()) <= 18 and kept.endswith('...') and long in out['sections'][0]['notes']


def test_pptx_one_visual_per_slide_with_text_beside_and_notes_kept():
    spec = {'title': 't', 'sections': [
        {'heading': 'Mixed', 'notes': 'say this', 'blocks': [
            para('intro'),
            {'type': 'chart', 'kind': 'bar', 'title': 'c', 'labels': ['a'], 'series': [{'name': 's', 'values': [1]}]},
            {'type': 'table', 'columns': ['x'], 'rows': [[1]]},
            {'type': 'code', 'lang': 'py', 'text': '\n'.join(f'line {i}' for i in range(20))}]},
        {'heading': '', 'blocks': [para('no heading')]}]}
    out, _ = normalize(spec, 'pptx')
    kinds = [[b['type'] for b in s['blocks']] for s in out['sections']]
    assert kinds == [['paragraph', 'chart'], ['table'], ['code'], ['code'], ['paragraph']]
    assert out['sections'][0]['notes'] == 'say this' and out['sections'][-1]['heading'] == 'Mixed (cont.)'
    assert out['sections'][2]['blocks'][0]['text'].count('\n') == 13


@pytest.mark.parametrize('fmt', FORMATS)
def test_normalize_is_idempotent(fmt):
    spec = {'title': 'Idem', 'sections': [
        {'heading': 'A', 'level': 2, 'notes': 'n', 'blocks': [
            para('Text with **bold** and a [link](https://a.b)'), {'type': 'bullets', 'items': [f'b{i}' for i in range(9)]},
            {'type': 'table', 'columns': ['x', 'y'], 'rows': [[i, '1,000'] for i in range(30)]},
            {'type': 'chart', 'kind': 'line', 'title': 'l', 'labels': ['a', 'b'], 'series': [{'name': 's', 'values': [1, 2]}]},
            {'type': 'quote', 'text': 'q', 'by': 'me'}, {'type': 'code', 'lang': 'sh', 'text': 'echo hi'}]}]}
    once, _ = normalize(spec, fmt)
    twice, res = normalize(once, fmt)
    assert twice == once


# ---------- from_markdown ----------

ANSWER = """# Solid-state batteries

Solid-state cells swap the **liquid** electrolyte for a solid one.

## Pros

- Higher energy density
- Safer: no flammable liquid
  that leaks
* Faster charging

## Numbers

| Metric | Li-ion | Solid-state |
|---|---:|:---:|
| Energy (Wh/kg) | 250 | 400 |
| Cost \\| kWh | 1,200 | n/a |

### Steps

1. Research
2) Pilot

```python
# not a heading
print("hi")
```

> The future is solid.
> — Some Engineer

---
Closing words.
"""


def test_from_markdown_parses_answers():
    spec = from_markdown(ANSWER)
    assert spec['title'] == 'Solid-state batteries'
    secs = spec['sections']
    assert [(s['heading'], s['level']) for s in secs] == [('', 1), ('Pros', 1), ('Numbers', 1), ('Steps', 2)]
    assert secs[0]['blocks'] == [para('Solid-state cells swap the **liquid** electrolyte for a solid one.')]
    assert secs[1]['blocks'][0] == {'type': 'bullets', 'ordered': False, 'items': [
        'Higher energy density', 'Safer: no flammable liquid that leaks', 'Faster charging']}
    table = secs[2]['blocks'][0]
    assert table['columns'] == ['Metric', 'Li-ion', 'Solid-state'] and table['rows'][1] == ['Cost | kWh', '1,200', 'n/a']
    steps = secs[3]['blocks']
    assert steps[0] == {'type': 'bullets', 'ordered': True, 'items': ['Research', 'Pilot']}
    assert steps[1] == {'type': 'code', 'lang': 'python', 'text': '# not a heading\nprint("hi")'}
    assert steps[2] == {'type': 'quote', 'text': 'The future is solid.', 'by': 'Some Engineer'}
    assert steps[3] == para('Closing words.')
    out, _ = normalize(spec, 'xlsx')
    assert out['sections'][2]['blocks'][0]['rows'][1] == ['Cost | kWh', 1200, 'n/a']


def test_from_markdown_titles_and_plain_text():
    assert from_markdown('just one line')['sections'] == [{'heading': '', 'level': 1, 'blocks': [para('just one line')]}]
    spec = from_markdown('## A\ntext\n## B\nmore', title='Given')
    assert spec['title'] == 'Given' and [s['heading'] for s in spec['sections']] == ['A', 'B']
    assert from_markdown('# One\n\nx\n\n# Two\n\ny')['title'] == 'One'  # two H1s: the first heading names the file
    assert from_markdown('')['sections'] == []


def test_from_markdown_keeps_to_l1_limits():
    long = '\n\n'.join(f'para {i}' for i in range(70))
    spec = from_markdown('# T\n\n' + long)
    assert [len(s['blocks']) for s in spec['sections']] == [30, 30, 10]
    many = '\n'.join(f'## H{i}\ntext {i}' for i in range(50))
    spec = from_markdown(many)
    assert len(spec['sections']) == 40 and len(spec['sections'][-1]['blocks']) <= 30
    normalize(spec, 'pdf')


def test_from_markdown_round_trips_markdown_output():
    from jevrouter.create import render
    spec = {'title': 'Round trip', 'subtitle': '', 'sections': [
        {'heading': 'Intro', 'level': 1, 'blocks': [para('Hello **world**, a *fine* day.'),
                                                    {'type': 'bullets', 'items': ['a', 'b'], 'ordered': False},
                                                    {'type': 'bullets', 'items': ['one', 'two'], 'ordered': True}]},
        {'heading': 'Detail', 'level': 2, 'blocks': [
            {'type': 'table', 'columns': ['k', 'v'], 'rows': [['pipe | here', 1], ['x', 2.5]]},
            {'type': 'quote', 'text': 'Quoted.', 'by': 'Ada'},
            {'type': 'code', 'lang': 'md', 'text': '```\n# fenced\n```'},
            para('# not a heading, - not a list')]}]}
    md = render(spec, 'md').decode()
    again = from_markdown(md)
    assert normalize(again, 'md')[0] == normalize(spec, 'md')[0]


# ---------- from_table ----------

CSV = 'region,month,units,revenue\nNorth,Jan,10,"1,000"\nSouth,Jan,30,2500\nNorth,Feb,20,1500.5\nSouth,Feb,n/a,3000\n'


def test_from_table_adds_an_aggregated_chart():
    cols, rows = to_table('csv', CSV)
    spec = from_table({'name': 'sales_2024.csv'}, rows, cols)
    assert spec['title'] == 'Sales 2024' and '4 rows from sales_2024.csv' in spec['subtitle']
    table, chart = spec['sections'][0]['blocks']
    assert table['columns'] == cols and len(table['rows']) == 4
    assert chart['kind'] == 'bar' and chart['labels'] == ['North', 'South']
    assert chart['series'] == [{'name': 'units', 'values': [30, 30]}, {'name': 'revenue', 'values': [2500.5, 5500]}]
    assert chart['title'] == 'Total units and revenue by region'
    out, _ = normalize(spec, 'xlsx')
    assert out['sections'][0]['blocks'][0]['rows'][0] == ['North', 'Jan', 10, 1000]


def test_from_table_time_series_line_and_no_chart_cases():
    spec = from_table({'name': 'trend.csv'}, [['2021', '5'], ['2022', '7'], ['2023', '9']], ['year', 'sales'],
                      title='Sales trend')
    chart = spec['sections'][0]['blocks'][1]
    assert spec['title'] == 'Sales trend' and chart['kind'] == 'line' and chart['labels'] == ['2021', '2022', '2023']
    assert len(from_table({}, [['a', 'b']], ['x', 'y'])['sections'][0]['blocks']) == 1  # no numbers
    assert len(from_table({}, [['1', '2']], ['x', 'y'])['sections'][0]['blocks']) == 1  # no labels
    names = [[f'name {i}', str(i)] for i in range(100)]
    assert len(from_table({}, names, ['name', 'score'])['sections'][0]['blocks']) == 1  # 100 distinct bars: no chart


def test_chart_summary_lines():
    bar = {'kind': 'bar', 'labels': ['a', 'b'], 'series': [{'name': 's', 'values': [1200, 5]}]}
    assert chart_summary(bar) == 'Highest a (1,200), lowest b (5).'
    pie = {'kind': 'pie', 'labels': ['a', 'b'], 'series': [{'name': 's', 'values': [3, 1]}]}
    assert chart_summary(pie) == 'Largest share: a (75%); smallest: b (25%).'
    assert '—' not in chart_summary(bar) and '–' not in chart_summary(pie)


# ---------- detect_format and file_name ----------

@pytest.mark.parametrize('text,fmt', [
    ('make a PDF report of this', 'pdf'),
    ('put that in a pdf', 'pdf'),
    ('slides on solid-state batteries', 'pptx'),
    ('make a deck about it', 'pptx'),
    ('turn this into a PowerPoint presentation', 'pptx'),
    ('turn this table into an Excel sheet', 'xlsx'),
    ('export to a spreadsheet', 'xlsx'),
    ('save it as XLSX', 'xlsx'),
    ('as a Word document please', 'docx'),
    ('export to word', 'docx'),
    ('give me a docx', 'docx'),
    ('save as markdown', 'md'),
    ('write it to an md file', 'md'),
    ('turn this pdf into slides', 'pptx'),
    ('slides from the attached pdf', 'pptx'),
    ('convert this deck to a PDF', 'pdf'),
    ('a balance sheet summary in excel', 'xlsx'),
    ('make a cheat sheet as a PDF', 'pdf'),
    ('summarise the attached pdf', None),
    ('what caused the landslide in 2024', None),
    ('write me a cheat sheet for git', None),
    ('what is the word count of this paragraph', None),
    ('explain the balance sheet', None),
    ('she has an MD from Harvard', None),
    ('slideshow of cats', None),
    ('', None),
])
def test_detect_format(text, fmt):
    assert detect_format(text) == fmt


@pytest.mark.parametrize('title,fmt,name', [
    ('Solid-state batteries: pros & cons', 'pdf', 'solid-state-batteries-pros-cons.pdf'),
    ('../../etc/passwd', 'md', 'etc-passwd.md'),
    ('a/b\\c:d', 'docx', 'a-b-c-d.docx'),
    ('Résumé für Zoë', 'pptx', 'resume-fur-zoe.pptx'),
    ('日本の電池', 'xlsx', 'document.xlsx'),
    ('', 'pdf', 'document.pdf'),
    ('   ', 'md', 'document.md'),
    ('CON', 'pdf', 'con-file.pdf'),
])
def test_file_name(title, fmt, name):
    assert file_name(title, fmt) == name


def test_file_name_length_and_format():
    long = file_name('word ' * 40, 'xlsx')
    assert len(long) <= 80 and long.endswith('.xlsx') and not long.startswith('-') and '--' not in long
    assert len(file_name('x' * 200, 'pdf')) == 80
    with pytest.raises(SpecError) as e:
        file_name('x', 'xlsm')
    assert e.value.rule_id == 'X1'


# ---------- review fixes: S4 HTML, money and percent, huge numbers ----------

@pytest.mark.parametrize('raw,want', [
    ('See &lt;img src=&quot;http://evil.example/p.png&quot;&gt; and &lt;script&gt;alert(1)&lt;/script&gt;ok', 'See and ok'),
    ('Intro &lt;img src=x onerror=alert(1)&gt; end', 'Intro end'),
    ('<details open ontoggle=alert(document.domain)>x</details>', 'x'),
    ('<image src="http://evil.example/i.png">hi', 'hi'),
    ('&#x3c;script&#x3e;', ''),
    ('&amp;lt;iframe src=//e.x&amp;gt;y', 'y'),
    ('a < b and c > d', 'a < b and c > d'),
    ('use `Vec<String>` here', 'use Vec<String> here'),
    ('![logo][1] and ![alt](http://e.x/i.png)', 'logo and alt'),
    ('AT&T <b>bold</b>', 'AT&T **bold**'),
])
def test_plain_decodes_entities_before_removing_every_tag(raw, want):
    assert plain(raw) == want


def test_money_and_percent_columns_become_numbers_with_a_format():
    spec = {'title': 'T', 'sections': [{'heading': 'H', 'blocks': [{'type': 'table', 'columns': ['R', 'Rev', 'M', 'Mixed'],
            'rows': [['N', '$1,200.50', '12%', '$5'], ['S', '$950', '8', '5%'], ['E', '-$3.00', '10.25%', 'n/a']]}]}]}
    out, res = normalize(spec, 'xlsx')
    t = out['sections'][0]['blocks'][0]
    assert t['rows'] == [['N', 1200.5, 0.12, '$5'], ['S', 950, 0.08, '5%'], ['E', -3.0, 0.1025, 'n/a']]
    assert t['formats'] == [None, '"$"#,##0.00', '0.00%', None]
    assert normalize(out, 'xlsx')[0]['sections'][0]['blocks'][0] == t  # idempotent: no second division by 100
    assert any(r.id == 'S5' and not r.ok for r in res)


def test_table_chart_leaves_out_percent_columns_beside_amounts_and_averages_rates():
    rows = [['North', '$1,200.50', '12%'], ['South', '$950.00', '8.5%'], ['North', '$300.00', '10%']]
    chart = from_table({'name': 'sales.csv'}, rows, ['Region', 'Revenue', 'Margin'])['sections'][0]['blocks'][1]
    assert [s['name'] for s in chart['series']] == ['Revenue'] and chart['series'][0]['values'] == [1500.5, 950.0]
    only_rates = from_table({'name': 'm.csv'}, [[r[0], r[2]] for r in rows], ['Region', 'Margin'])
    chart = only_rates['sections'][0]['blocks'][1]
    assert chart['title'].startswith('Average Margin') and chart['series'][0]['values'] == [11.0, 8.5]


def test_integers_too_big_for_a_float_do_not_crash_normalize():
    spec = {'title': 'T', 'sections': [{'heading': 'H', 'blocks': [
        {'type': 'table', 'columns': ['a', 'b'], 'rows': [['x', 10 ** 400]]},
        {'type': 'chart', 'kind': 'bar', 'title': 'c', 'labels': ['x', 'y'], 'series': [{'name': 's', 'values': [10 ** 400, 2]}]}]}]}
    for fmt in FORMATS:
        out, _ = normalize(spec, fmt)
    blocks = normalize(spec, 'pdf')[0]['sections'][0]['blocks']
    assert blocks[0]['rows'] == [['x', str(10 ** 400)]] and blocks[1]['series'][0]['values'] == [None, 2]


def test_schema_is_portable_across_engines():
    """Gemini (Antigravity) rejects enums that aren't strings, which Claude and Codex accept: keep every enum a string
    list so one schema works on every engine."""
    def enums(o):
        if isinstance(o, dict):
            if 'enum' in o:
                yield o
            for v in o.values():
                yield from enums(v)
        elif isinstance(o, list):
            for v in o:
                yield from enums(v)
    found = list(enums(DOCSPEC_SCHEMA))
    assert found and all(e.get('type') == 'string' and all(isinstance(x, str) and x for x in e['enum']) for e in found)
