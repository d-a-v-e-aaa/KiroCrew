/**
 * The MCP Servers page's Try a tool panel: hidden while `mcp.tool_tester` is off,
 * a form from the input schema, and a confirm before a tool not declared read-only.
 * `i18nT` is mocked to the key so assertions pin keys.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

const { config, tryTool } = vi.hoisted(() => ({ config: vi.fn(), tryTool: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: (...a: unknown[]) => config(...a), mcpTryTool: (...a: unknown[]) => tryTool(...a) },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return { ...orig, i18nT: (key: string) => key }
})

import McpTryTool, { argumentsFrom, schemaFields } from './McpTryTool'

const WRITE_TOOL = {
  name: 'write',
  description: 'Writes a note',
  inputSchema: { type: 'object', properties: { text: { type: 'string' }, count: { type: 'integer' } }, required: ['text'] },
  needsConfirm: true,
  switchedOff: false,
}

beforeEach(() => { config.mockReset(); tryTool.mockReset() })

describe('schemaFields and argumentsFrom', () => {
  it('builds fields for the simple types and falls back for anything else', () => {
    expect(schemaFields({ type: 'object', properties: { a: { type: 'string' }, b: { enum: ['x', 'y'] } }, required: ['a'] })).toEqual([
      { name: 'a', kind: 'string', options: [], required: true },
      { name: 'b', kind: 'enum', options: ['x', 'y'], required: false },
    ])
    expect(schemaFields({ type: 'object', properties: { a: { type: 'array' } } })).toBeNull()
    expect(schemaFields({})).toEqual([])
  })

  it('types the values, leaves out an empty optional one and names a bad one', () => {
    const fields = schemaFields(WRITE_TOOL.inputSchema)!
    expect(argumentsFrom(fields, { text: 'hi', count: '3' })).toEqual({ text: 'hi', count: 3 })
    expect(argumentsFrom(fields, { text: 'hi' })).toEqual({ text: 'hi' })
    expect(argumentsFrom(fields, { text: 'hi', count: '1.5' })).toEqual({ invalid: 'count' })
    expect(argumentsFrom(fields, {})).toEqual({ invalid: 'text' })
  })
})

describe('McpTryTool', () => {
  it('draws nothing while the setting is off', async () => {
    config.mockResolvedValue({ mcp: { tool_tester: false } })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    await waitFor(() => expect(config).toHaveBeenCalled())
    expect(screen.queryByTestId('mcp-try-tool')).toBeNull()
  })

  it('asks before running a tool not declared read-only, then sends the confirm', async () => {
    config.mockResolvedValue({ mcp: { tool_tester: true } })
    tryTool.mockResolvedValueOnce({ ok: true, tools: [WRITE_TOOL] })
    tryTool.mockResolvedValueOnce({ ok: true, result: '{"content": []}', isError: false, truncated: true })
    renderWithProviders(<McpTryTool servers={['notes']} />)
    fireEvent.click(await screen.findByText('pages.overview.mcpTryTool.load_tools'))
    expect(await screen.findByText('Writes a note')).toBeTruthy()
    expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes' })

    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'hello' } })
    fireEvent.click(screen.getByText('pages.overview.mcpTryTool.run'))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(tryTool).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByText('pages.overview.mcpTryTool.run_anyway'))
    await waitFor(() => expect(tryTool).toHaveBeenCalledTimes(2))
    expect(tryTool).toHaveBeenLastCalledWith({ server: 'notes', tool: 'write', arguments: { text: 'hello' }, confirm: true })
    expect((await screen.findByTestId('mcp-try-tool-result')).textContent).toContain('{"content": []}')
    expect(screen.getByText('pages.overview.mcpTryTool.truncated')).toBeTruthy()
  })
})
