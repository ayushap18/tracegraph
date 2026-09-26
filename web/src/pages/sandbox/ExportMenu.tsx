import { Button } from '../../ui'
import { Icon } from '../../icons'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { download, stamp, toJSON, toMarkdown } from './export'
import type { ExportInput } from './types'

export interface ExportMenuProps { input: ExportInput; disabled?: boolean }

export function ExportMenu({ input, disabled }: ExportMenuProps) {
  const save = (kind: 'md' | 'json') => {
    const name = `tracegraph-sandbox-${stamp()}.${kind}`
    if (kind === 'md') download(name, toMarkdown(input), 'text/markdown;charset=utf-8')
    else download(name, toJSON(input), 'application/json;charset=utf-8')
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button variant="secondary" size="sm" icon="download" iconRight="chevron-down" disabled={disabled}>Export</Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60 rounded-lg">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Download this conversation</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => save('md')}>
          <Icon name="file-text" size={15} />Markdown (.md)
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => save('json')}>
          <Icon name="file-json" size={15} />JSON (.json)
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <p className="m-0 flex items-start gap-1.5 px-2 py-1.5 text-xs leading-relaxed text-muted-foreground">
          <Icon name="lock" size={12} className="mt-0.5 shrink-0" />
          The file is saved on your computer only. Nothing is stored on the server.
        </p>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default ExportMenu
