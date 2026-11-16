import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { api } from '../../api/client'
import type { McpTryToolAnswer, McpTryToolRow } from '../../api/client/mcp'
import { Card, Btn, Input } from '../../components/ui'
import { NativeSelect, NativeSelectOption } from '../../components/ui/native-select'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/** One argument of a simple type; a schema with any other shape gets the raw JSON box. */
export type ToolField = { name: string; kind: string; options: string[]; required: boolean }

/** The form for an input schema, or `null` when it needs the raw JSON box. */
export function schemaFields(schema: Record<string, unknown>): ToolField[] | null {
  const props = (schema.properties ?? {}) as Record<string, { type?: unknown; enum?: unknown }>
  if ((schema.type ?? 'object') !== 'object' || typeof props !== 'object' || props === null) return null
  const required = Array.isArray(schema.required) ? schema.required : []
  const fields: ToolField[] = []
  for (const [name, p] of Object.entries(props)) {
    const options = Array.isArray(p?.enum) && p.enum.every(v => typeof v === 'string') ? (p.enum as string[]) : null
    const kind = options ? 'enum' : p?.type
    if (typeof kind !== 'string' || !['enum', 'string', 'number', 'integer', 'boolean'].includes(kind)) return null
    fields.push({ name, kind, options: kind === 'boolean' ? ['true', 'false'] : options ?? [], required: required.includes(name) })
  }
  return fields
}

/** The arguments for filled-in values; an empty optional field is left out, a bad one named. */
export function argumentsFrom(fields: ToolField[], values: Record<string, string>): Record<string, unknown> | { invalid: string } {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const v = (values[f.name] ?? '').trim()
    const n = Number(v)
    if (!v && f.required) return { invalid: f.name }
    if (!v) continue
    if ((f.kind === 'number' || f.kind === 'integer') && (!Number.isFinite(n) || (f.kind === 'integer' && !Number.isInteger(n)))) return { invalid: f.name }
    out[f.name] = f.kind === 'boolean' ? v === 'true' : f.kind === 'number' || f.kind === 'integer' ? n : v
  }
  return out
}

/** The MCP Servers page's Try a tool panel, drawn only while `mcp.tool_tester` is on. */
export default function McpTryTool({ servers }: { servers: string[] }) {
  const { data: cfg } = useQuery<{ mcp?: { tool_tester?: boolean } }>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  return cfg?.mcp?.tool_tester === true ? <TryToolPanel servers={servers} /> : null
}

function TryToolPanel({ servers }: { servers: string[] }) {
  const [server, setServer] = useState(servers[0] ?? '')
  const [tools, setTools] = useState<McpTryToolRow[] | null>(null)
  const [toolName, setToolName] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [raw, setRaw] = useState('{}')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [answer, setAnswer] = useState<McpTryToolAnswer | null>(null)
  const [error, setError] = useState('')
  const tool = tools?.find(x => x.name === toolName)
  const fields = tool ? schemaFields(tool.inputSchema) : null

  const send = async (body: Parameters<typeof api.mcpTryTool>[0]) => {
    setBusy(true); setError(''); setAnswer(null); setConfirming(false)
    try { return await api.mcpTryTool(body) } catch (e) { setError((e as Error).message); return null } finally { setBusy(false) }
  }
  const pick = (name: string) => { setToolName(name); setValues({}); setRaw('{}'); setConfirming(false); setAnswer(null) }
  const load = async () => { setTools(null); const got = await send({ server }); if (got?.tools) { setTools(got.tools); pick(got.tools[0]?.name ?? '') } }
  const run = async (confirm: boolean) => {
    if (!tool) return
    let args: Record<string, unknown> | { invalid: string } | null = null
    try { args = fields ? argumentsFrom(fields, values) : JSON.parse(raw) } catch { args = null }
    if (args === null || typeof args !== 'object' || Array.isArray(args)) return setError(i18nT('pages.overview.mcpTryTool.invalid_json'))
    if (fields && typeof args.invalid === 'string') return setError(i18nT('pages.overview.mcpTryTool.invalid_field', { name: args.invalid }))
    if (tool.needsConfirm && !confirm) return setConfirming(true)
    setAnswer(await send({ server, tool: tool.name, arguments: args, confirm }))
  }

  const box = 'rounded-md border border-border bg-bg px-3 py-1.5 font-mono text-[12px] text-text focus-ring-accent'
  return (
    <Card className="mt-4" data-testid="mcp-try-tool">
      <h4 className="text-sm font-semibold text-text-strong m-0 mb-1">{i18nT('pages.overview.mcpTryTool.title')}</h4>
      <p className="text-xs text-muted m-0 mb-3">{i18nT('pages.overview.mcpTryTool.help')}</p>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <NativeSelect value={server} onChange={e => { setServer(e.target.value); setTools(null) }} aria-label={i18nT('pages.overview.mcpTryTool.server')}>
          {servers.map(s => <NativeSelectOption key={s} value={s}>{s}</NativeSelectOption>)}
        </NativeSelect>
        <Btn onClick={load} busy={busy && !tools} disabled={!server || busy}>{i18nT('pages.overview.mcpTryTool.load_tools')}</Btn>
      </div>
      {tools?.length === 0 && <p className="text-xs text-muted m-0">{i18nT('pages.overview.mcpTryTool.no_tools')}</p>}
      {tools && tools.length > 0 && (
        <div className="flex flex-col gap-2">
          <NativeSelect value={toolName} onChange={e => pick(e.target.value)} aria-label={i18nT('pages.overview.mcpTryTool.tool')}>
            {tools.map(x => <NativeSelectOption key={x.name} value={x.name} disabled={x.switchedOff}>{x.name}</NativeSelectOption>)}
          </NativeSelect>
          {tool?.description && <p className="text-xs text-muted m-0 whitespace-pre-wrap">{tool.description}</p>}
          {fields ? fields.map(f => (
            <label key={f.name} className="flex items-center gap-2 text-[12px]">
              <span className="w-40 font-mono truncate" title={f.name}>{f.name}{f.required ? ' *' : ''}</span>
              {f.options.length > 0 ? (
                <NativeSelect value={values[f.name] ?? ''} onChange={e => setValues({ ...values, [f.name]: e.target.value })}>
                  <NativeSelectOption value="">{i18nT('pages.overview.mcpTryTool.not_set')}</NativeSelectOption>
                  {f.options.map(o => <NativeSelectOption key={o} value={o}>{o}</NativeSelectOption>)}
                </NativeSelect>
              ) : <Input value={values[f.name] ?? ''} onChange={e => setValues({ ...values, [f.name]: e.target.value })} />}
            </label>
          )) : <textarea value={raw} onChange={e => setRaw(e.target.value)} spellCheck={false} aria-label={i18nT('pages.overview.mcpTryTool.raw_arguments')} className={`w-full min-h-[96px] resize-y ${box}`} />}
          {confirming ? (
            <div className="rounded-md border border-warn/40 bg-bg-elevated px-3 py-2 text-[12px]" role="alert">
              <AlertTriangle className="lucide-inline text-warn" /> {i18nT('pages.overview.mcpTryTool.confirm_text')}
              <div className="flex gap-2 mt-2">
                <Btn danger onClick={() => run(true)} disabled={busy}>{i18nT('pages.overview.mcpTryTool.run_anyway')}</Btn>
                <Btn onClick={() => setConfirming(false)}>{i18nT('pages.overview.mcpTryTool.cancel')}</Btn>
              </div>
            </div>
          ) : <div><Btn primary onClick={() => run(false)} busy={busy} disabled={!tool || busy}>{i18nT('pages.overview.mcpTryTool.run')}</Btn></div>}
        </div>
      )}
      {/* No hand-off: the arguments are typed here for one run, and an agent in the
          chat has no copy of them to retry with. */}
      {error && <ErrorNotice className="mt-3" message={error} testId="mcp-try-tool-error" />}
      {answer?.result !== undefined && (
        <div className="mt-3" data-testid="mcp-try-tool-result">
          {answer.isError && <p className="text-xs text-danger m-0 mb-1">{i18nT('pages.overview.mcpTryTool.tool_error')}</p>}
          <pre className={`max-h-[360px] overflow-auto whitespace-pre-wrap break-all m-0 ${box}`}>{answer.result}</pre>
          {answer.truncated && <p className="text-xs text-muted m-0 mt-1">{i18nT('pages.overview.mcpTryTool.truncated')}</p>}
        </div>
      )}
    </Card>
  )
}
