/**
 * Kiro Space store rows read for one account never show for another: the
 * block, the rail count, the listing page and Install, after a sign-out, a
 * switch in the CLI, or an answer that lands late.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, renderHook } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import '../i18n/all'

const api = vi.hoisted(() => ({
  getMarketRegistries: vi.fn(),
  setMarketRegistries: vi.fn(),
  marketSignIn: vi.fn(),
  marketSignOut: vi.fn(),
  listMarketListings: vi.fn(),
  getMarketListing: vi.fn(),
  prepareMarketInstall: vi.fn(),
  confirmMarketInstall: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

import { newQueryClient } from '../api/queryClient'
import MarketRegistryManager from '../components/appstore/MarketRegistryManager'
import { MarketStoreSections, useMarketSourceRows } from '../components/appstore/MarketListingRows'
import MarketListingPage from '../pages/apps/MarketListingPage'

const OFFER = { id: 'team-store', label: 'Team store', web: 'https://store.example' }
const ALICE = { state: 'signed_in', account: 'alice' }
const BOB = { state: 'signed_in', account: 'bob' }
const OUT = { state: 'needs_sign_in', account: '' }
const regs = (status: object) => ({ offers: [OFFER], added: ['team-store'], status: { 'team-store': status } })
const ROW = {
  registry: 'team-store', ref: 'alice/private-a', name: 'private-a', displayName: 'Private A',
  summary: 'only alice sees this', category: 'productivity', tags: [], publisher: 'alice',
  latestVersion: 2, latestSemver: '1.1.0', trust: 'COMMUNITY', official: false, canPublish: false,
  rating: { status: 'absent' }, ratingCount: { status: 'ok', value: 0 }, downloads: { status: 'absent' },
  installed: false, installedVersion: null, nameTaken: false,
}
const listA = { ok: true, registry: 'team-store', account: 'alice', rows: [ROW], truncated: false, status: ALICE }
const listB = { ok: true, registry: 'team-store', account: 'bob', rows: [], truncated: false, status: BOB }
const listOut = { ok: true, registry: 'team-store', account: '', rows: [], truncated: false, status: OUT }

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function mount(client = newQueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<><MarketRegistryManager /><MarketStoreSections /></>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

beforeEach(() => { for (const fn of Object.values(api)) fn.mockReset() })

describe('store rows follow the signed-in account', () => {
  it('drops alice rows, counts and Install once the status read shows bob', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.listMarketListings.mockResolvedValue(listA)
    const client = mount()
    expect(await screen.findByText('Private A')).toBeInTheDocument()
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const rail = renderHook(() => useMarketSourceRows(), { wrapper })
    await waitFor(() => expect(rail.result.current[0]?.count).toBe(1))
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    api.listMarketListings.mockResolvedValue(listB)
    await act(async () => { await client.invalidateQueries({ queryKey: ['market-registries'] }) })
    await waitFor(() => expect(screen.getByText(/bob/)).toBeInTheDocument())
    await waitFor(() => expect(api.listMarketListings).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Private A')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    await waitFor(() => expect(rail.result.current[0]?.count).toBe(0))
  })

  it('shows no alice row when the first read lands after a sign-out', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    const first = deferred<typeof listA>()
    api.listMarketListings.mockReturnValueOnce(first.promise).mockResolvedValue(listOut)
    api.marketSignOut.mockResolvedValue({ ok: true, registry: 'team-store', state: 'signed_out' })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    api.getMarketRegistries.mockResolvedValue(regs(OUT))
    await waitFor(() => expect(api.marketSignOut).toHaveBeenCalled())
    await act(async () => { first.resolve(listA) })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    expect(screen.queryByText('Private A')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })

  it('does not let a late Refresh put alice rows back after a sign-out', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    const later = deferred<typeof listA>()
    api.listMarketListings.mockResolvedValueOnce(listA).mockReturnValueOnce(later.promise).mockResolvedValue(listOut)
    api.marketSignOut.mockResolvedValue({ ok: true, registry: 'team-store', state: 'signed_out' })
    mount()
    expect(await screen.findByText('Private A')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    api.getMarketRegistries.mockResolvedValue(regs(OUT))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText('Private A')).toBeNull())
    await act(async () => { later.resolve(listA) })
    expect(screen.queryByText('Private A')).toBeNull()
  })

  it("does not let alice's late Refresh replace bob's answer after a CLI switch", async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    const later = deferred<typeof listA>()
    api.listMarketListings.mockResolvedValueOnce(listA).mockReturnValueOnce(later.promise).mockResolvedValue(listB)
    const client = mount()
    expect(await screen.findByText('Private A')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    await act(async () => { await client.invalidateQueries({ queryKey: ['market-registries'] }) })
    await waitFor(() => expect(api.listMarketListings).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(screen.queryByText('Private A')).toBeNull())
    await act(async () => { later.resolve(listA) })
    expect(screen.queryByText('Private A')).toBeNull()
  })

  it('drops the listing page of alice after a sign-out, and on reopen', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(ALICE))
    api.getMarketListing.mockResolvedValueOnce({ ok: true, registry: 'team-store', account: 'alice', listing: ROW, status: ALICE })
      .mockResolvedValue({ ok: true, registry: 'team-store', account: '', listing: null, status: OUT })
    api.marketSignOut.mockResolvedValue({ ok: true, registry: 'team-store', state: 'signed_out' })
    api.listMarketListings.mockResolvedValue(listOut)
    const client = newQueryClient()
    const page = (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/alice/private-a']}>
          <MarketRegistryManager />
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    const view = render(page)
    expect(await screen.findByText('Private A')).toBeInTheDocument()
    api.getMarketRegistries.mockResolvedValue(regs(OUT))
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText('Private A')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    view.unmount()
    render(page)
    expect((await screen.findAllByText('Added · not signed in')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Private A')).toBeNull()
  })

  it('shows an answer read for another account than the one signed in now as nothing', async () => {
    api.getMarketRegistries.mockResolvedValue(regs(BOB))
    api.listMarketListings.mockResolvedValue(listA)
    mount()
    await waitFor(() => expect(api.listMarketListings).toHaveBeenCalled())
    await waitFor(() => expect(api.getMarketRegistries.mock.calls.length).toBeGreaterThan(1))
    expect(screen.queryByText('Private A')).toBeNull()
  })
})
