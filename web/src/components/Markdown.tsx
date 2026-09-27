import type { ReactNode } from 'react'
import { safeHref } from '../lib'

// A small, safe Markdown subset for agent answers (LLM engines write Markdown; keyless agents write plain text).
// Everything is built as React elements, never HTML strings: bold, italics, inline code, links, fenced code,
// bullet/numbered lists, headings and pipe tables (the Table answer style).

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s)]+)/g
  let last = 0, m: RegExpExecArray | null, i = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const k = `${key}-${i++}`
    if (m[1]) out.push(<code key={k}>{m[1].slice(1, -1)}</code>)
    else if (m[2]) out.push(<strong key={k}>{m[2].slice(2, -2)}</strong>)
    else if (m[3]) out.push(<em key={k}>{m[3].slice(1, -1)}</em>)
    else if (m[4]) out.push(<a key={k} href={safeHref(m[5])} target="_blank" rel="noopener noreferrer">{m[4]}</a>)
    else if (m[6]) out.push(<a key={k} href={safeHref(m[6])} target="_blank" rel="noopener noreferrer">{m[6]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/
const isRow = (l: string) => l.includes('|') && l.trim() !== ''
const tableAt = (lines: string[], i: number) => isRow(lines[i]) && i + 1 < lines.length && lines[i + 1].includes('|') && TABLE_RULE.test(lines[i + 1])
const cells = (l: string) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())

export default function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  const lines = text.split('\n')
  let i = 0, n = 0
  while (i < lines.length) {
    const line = lines[i]
    const key = 'b' + n++
    if (line.startsWith('```')) {
      const body: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) body.push(lines[i++])
      i++ // closing fence (or end of a still-streaming block)
      blocks.push(<pre key={key} className="md-code"><code>{body.join('\n')}</code></pre>)
      continue
    }
    // A pipe table: a header row, a |---|:--:| rule, then body rows. Wide tables scroll inside their own box.
    if (tableAt(lines, i)) {
      const head = cells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && isRow(lines[i])) rows.push(cells(lines[i++]))
      blocks.push(
        <div key={key} className="max-w-full overflow-x-auto rounded-md border border-border">
          <table className="w-full border-collapse text-left text-[14px] leading-snug">
            <thead className="bg-subtle/60">
              <tr>{head.map((c, j) => <th key={j} scope="col" className="border-b border-border px-3 py-2 font-semibold text-foreground">{inline(c, `${key}h${j}`)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className="border-t border-border first:border-t-0">
                  {head.map((_, j) => <td key={j} className="px-3 py-2 align-top">{inline(r[j] ?? '', `${key}r${ri}c${j}`)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }
    const list = /^\s*(?:[-*•]|\d+[.)])\s+/
    if (list.test(line)) {
      const ordered = /^\s*\d/.test(line)
      const items: ReactNode[] = []
      while (i < lines.length && list.test(lines[i])) {
        items.push(<li key={i}>{inline(lines[i].replace(list, ''), key + i)}</li>)
        i++
      }
      blocks.push(ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>)
      continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) { blocks.push(<p key={key} className="md-h">{inline(h[2], key)}</p>); i++; continue }
    if (!line.trim()) { i++; continue }
    const para: string[] = []
    while (i < lines.length && lines[i].trim() && !lines[i].startsWith('```') && !list.test(lines[i]) && !/^#{1,4}\s/.test(lines[i]) && !tableAt(lines, i)) para.push(lines[i++])
    blocks.push(<p key={key}>{para.flatMap((l, j) => (j ? [<br key={'br' + j} />, ...inline(l, key + j)] : inline(l, key + j)))}</p>)
  }
  return <div className="md">{blocks}</div>
}
