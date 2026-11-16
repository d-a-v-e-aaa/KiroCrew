/**
 * One-click edit shortcuts ("Quick edits" in the toolbar's More menu).
 *
 * Each item sends ONE preset instruction to the artifact's companion session as an
 * ordinary turn; the agent then changes the artifact through its own
 * `artifact_update`. Pinned here:
 * - nothing is sent until an item is picked, and nothing is sent while offline;
 * - a selection inside the rendered body scopes the instruction to that passage,
 *   no selection scopes it to the whole document;
 * - the scope label says the agent rewrites it and the result is saved as a new version;
 * - a passage picked with the mouse survives the selection box taking focus;
 * - a half-written sidebar comment is asked about before the chat panel replaces it;
 * - a failed session lookup shows its error and never creates a second session;
 * - with no bound session the shortcut opens one and sends there;
 * - widgets, apps and the other non-document kinds show no such menu, as on main.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { sseSlots, sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { editShortcutPrompt, usableEditQuote } from '../components/artifactEditShortcuts.prompt'
import type { Artifact, ChatSlot } from '../types'
import { chooseMore, commentsShown, openMore } from './artifactMoreMenu'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Intro\n\nThe quick brown fox jumps over the lazy dog.',
  ...overrides,
})

const BOUND = { key: 'chat-bound', title: 'Artifact: CR Queue', messages: 3, running: false, artifact: 'cr-queue' } as ChatSlot
const accepted = () => ({ ok: true, json: () => Promise.resolve({ ok: true }) })

/** `cold`: the slots list has not loaded yet, so the page must ask the server before it decides. */
function renderPage(artifact: Artifact, opts: { bound?: boolean; connected?: boolean; cold?: boolean; popout?: boolean } = {}) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  const store = createTestStore()
  act(() => {
    store.dispatch(opts.connected === false ? sseDisconnected() : sseConnected())
    if (!opts.cold) store.dispatch(sseSlots(opts.bound === false ? [] : [BOUND]))
  })
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage popout={opts.popout} />} />
    </Routes>,
    { route: '/artifacts/cr-queue', store },
  )
}

/** Open More, then the Quick edits submenu; resolves the submenu's items by name. */
async function openQuickEdits() {
  openMore()
  const trigger = await screen.findByRole('menuitem', { name: 'Quick edits' })
  fireEvent.click(trigger)
  await screen.findByRole('menuitem', { name: 'Shorter' })
}

/** The DOM selection a mouse drag over `word` leaves, then the mouseup the selection toolbar listens for. */
function selectWithMouse(word: string) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node: Node | null = null
  while (walker.nextNode()) {
    if ((walker.currentNode.textContent ?? '').includes(word)) { node = walker.currentNode; break }
  }
  if (!node) throw new Error('word not in the rendered body')
  const start = (node.textContent ?? '').indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  // The test DOM has no layout; the toolbar reads the rect only to place its box.
  range.getBoundingClientRect = () => ({
    left: 10, top: 10, bottom: 30, right: 60, width: 50, height: 20, x: 10, y: 10, toJSON: () => ({}),
  }) as DOMRect
  const live = {
    isCollapsed: false, anchorNode: node, rangeCount: 1, getRangeAt: () => range,
    removeAllRanges: () => undefined, addRange: () => undefined, toString: () => word,
  } as unknown as Selection
  const spy = vi.spyOn(window, 'getSelection').mockReturnValue(live)
  const host = (node as Node).parentElement as HTMLElement
  fireEvent.mouseDown(host)
  fireEvent.mouseUp(host)
  /** What focus moving into the selection box's input does to the real selection. */
  const collapse = () => spy.mockReturnValue({
    isCollapsed: true, anchorNode: null, rangeCount: 0, getRangeAt: () => { throw new Error('no range') },
    removeAllRanges: () => undefined, addRange: () => undefined, toString: () => '',
  } as unknown as Selection)
  return { collapse }
}

const COMPOSER_INPUT = 'Comment on the selected text'
const SIDEBAR_BOX = 'Add a comment on the whole artifact…'

/** Open the sidebar and leave an unsent comment in its add box. */
async function leaveSidebarDraft() {
  if (!(await commentsShown())) await chooseMore(/Show comments/)
  fireEvent.click(await screen.findByRole('button', { name: /^Add comment$/ }))
  fireEvent.change(screen.getByPlaceholderText(SIDEBAR_BOX), { target: { value: 'unsent note' } })
}

describe('ArtifactDetailPage edit shortcuts', () => {
  afterEach(() => { vi.restoreAllMocks() })
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    window.getSelection()?.removeAllRanges()
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).sandboxDocUrl = vi.fn().mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([BOUND])
    vi.mocked(api).chatSlotDetail = vi.fn().mockResolvedValue({ messages: [], has_more: false, total: 0 })
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue(accepted())
  })

  it('sends nothing until an item is picked', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it('applies a shortcut to the whole document when nothing is selected', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites the whole document · saved as a new version')).toBeInTheDocument()
    expect(screen.queryByText(/Applies to/)).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Proofread' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message, slot] = vi.mocked(api).sendChat.mock.calls[0]
    expect(slot).toBe('chat-bound')
    expect(message).toContain('Proofread the whole document.')
    expect(message).toContain('slug `cr-queue`')
    expect(message).toContain('artifact_update')
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('scopes a shortcut to the selected passage', async () => {
    renderPage(mkArtifact())
    const body = await screen.findByText(/The quick brown fox/)
    const range = document.createRange()
    range.selectNodeContents(body)
    window.getSelection()?.addRange(range)
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites the selected text · saved as a new version')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message] = vi.mocked(api).sendChat.mock.calls[0]
    expect(message).toContain('Make the selected passage shorter.')
    expect(message).toContain(JSON.stringify('The quick brown fox jumps over the lazy dog.'))
  })

  it('sends the tone presets from the Change tone submenu', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Change tone' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'More formal' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][0]).toContain('more formal tone')
  })

  it('opens a companion session when none is bound and sends there', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    renderPage(mkArtifact(), { bound: false })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Longer' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('slot-new')
  })

  it('marks the Quick edits row with a pen, not the chat toggle sparkle, and shows chevrons on both submenu rows', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    const quick = screen.getByRole('menuitem', { name: 'Quick edits' })
    expect(quick.querySelector('svg.lucide-pen-line')).not.toBeNull()
    expect(quick.querySelector('svg.lucide-sparkles')).toBeNull()
    expect(quick.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Change tone' }).querySelector('svg.lucide-chevron-right')).not.toBeNull()
  })

  it('keeps the passage picked with the mouse after the selection box takes focus', async () => {
    renderPage(mkArtifact())
    await screen.findByText(/The quick brown fox/)
    const { collapse } = selectWithMouse('brown fox')
    await screen.findByLabelText(COMPOSER_INPUT)
    collapse()
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites the selected text · saved as a new version')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message] = vi.mocked(api).sendChat.mock.calls[0]
    expect(message).toContain('Make the selected passage shorter.')
    expect(message).toContain(JSON.stringify('brown fox'))
  })

  it.each([false, true])('asks before a half-written sidebar comment is replaced by the chat (popout %s)', async (popout) => {
    renderPage(mkArtifact(), { popout })
    await screen.findByText('CR Queue')
    await leaveSidebarDraft()
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    const dialog = await screen.findByRole('dialog')
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByPlaceholderText(SIDEBAR_BOX)).toHaveValue('unsent note')
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-page')).toBeNull()
  })

  it('sends once the half-written sidebar comment is discarded', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await leaveSidebarDraft()
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
  })

  it('asks nothing when the sidebar holds no draft', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    if (!(await commentsShown())) await chooseMore(/Show comments/)
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows the error and creates no second session when the session lookup fails', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockRejectedValue(new Error('slots unavailable'))
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    expect(await screen.findByText('slots unavailable')).toBeInTheDocument()
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it('sends to the session the lookup finds on a cold page, and creates none', async () => {
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('chat-bound')
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('creates a session only after a successful lookup that found none', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('slot-new')
  })

  it('sends nothing while the gateway is disconnected', async () => {
    renderPage(mkArtifact(), { connected: false })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await act(async () => { await Promise.resolve() })
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it.each(['widget', 'webapp', 'json', 'svg', 'image'] as const)('offers no Quick edits on a %s artifact', async (kind) => {
    renderPage(mkArtifact({ kind, content: kind === 'json' ? '{}' : '<div>x</div>' }))
    await screen.findByText('CR Queue')
    openMore()
    await screen.findByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Quick edits' })).toBeNull()
  })
})

describe('edit shortcut prompt', () => {
  it('drops a blank or over-long selection so the shortcut falls back to the whole document', () => {
    expect(usableEditQuote(undefined)).toBe('')
    expect(usableEditQuote('   \n ')).toBe('')
    expect(usableEditQuote('x'.repeat(4001))).toBe('')
    expect(usableEditQuote('  keep me  ')).toBe('keep me')
  })

  it('quotes the passage as a JSON string so it cannot break out of the instruction', () => {
    const text = editShortcutPrompt('shorter', 'Doc', 'doc', 'say "hi"\nand stop')
    expect(text).toContain(JSON.stringify('say "hi"\nand stop'))
    expect(text).not.toContain('\n')
  })
})
