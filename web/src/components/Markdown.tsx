import type { ReactNode } from 'react'
import { safeHref } from '../lib'

// A small, safe Markdown subset for agent answers (Claude writes Markdown; keyless agents write plain text).
// Everything is built as React elements, never HTML strings: bold, italics, inline code, links, fenced code,
// bullet/numbered lists and headings.

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
    while (i < lines.length && lines[i].trim() && !lines[i].startsWith('```') && !list.test(lines[i]) && !/^#{1,4}\s/.test(lines[i])) para.push(lines[i++])
    blocks.push(<p key={key}>{para.flatMap((l, j) => (j ? [<br key={'br' + j} />, ...inline(l, key + j)] : inline(l, key + j)))}</p>)
  }
  return <div className="md">{blocks}</div>
}
