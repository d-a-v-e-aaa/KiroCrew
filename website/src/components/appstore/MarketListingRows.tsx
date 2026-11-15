/**
 * MarketListingRows — the apps of one added Kiro Space store, on the Discover page.
 *
 * Its own block, never mixed into the git registry list: every action here is
 * keyed by the store id and the listing ref, never by the app name, so a store
 * listing and a git row that share a name stay two cards that cannot trigger
 * each other's install. Install opens InstallFromSpaceDialog
 * (`POST /api/apps/market/install`); the row opens
 * `/apps/-/market/<store>/<namespace>/<name>`.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { BadgeCheck, Check, RefreshCw, Star } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import Clickable from '../Clickable'
import { Btn, IconButton } from '../ui'
import AppIconTile from './AppIconTile'
import InstallFromSpaceDialog from './InstallFromSpaceDialog'
import { MARKET_REGISTRIES_QUERY_KEY, useMarketStatusText } from './MarketRegistryManager'
import { fmtNumber } from '../../i18n/format'
import { cardDataKey } from '../../pages/apps/cardDataKey'
import {
  sourceRowKey, type MarketListingResponse, type MarketListingRow, type MarketListingsResponse,
  type MarketRegistriesResponse, type MarketRegistryOffer, type MarketRegistryStatus,
} from './types'
import { categoryFor } from './categories'
import type { SourceRow } from './CategoryRail'

/** Most rows the gateway reads from one store (10 pages of 100). */
export const MARKET_LISTING_LIMIT = 1000

/** A store's listing query, per store and signed-in account: one account's rows never sit under another's key. */
export const marketListingsKey = (registry: string, account: string) => ['market-listings', registry, account] as const
/** One listing's query, per store, account and ref. */
export const marketListingKey = (registry: string, account: string, ref: string) =>
  ['market-listing', registry, account, ref] as const

/** The account a store status says is signed in; `''` for every other state. */
export function statusAccount(status: MarketRegistryStatus | undefined): string {
  return status?.state === 'signed_in' ? status.account : ''
}

/** Drop a store's list and detail queries kept for any account but *account*. */
export function dropOtherAccounts(queryClient: QueryClient, registry: string, account: string): void {
  for (const root of ['market-listings', 'market-listing']) {
    queryClient.removeQueries({ queryKey: [root, registry], predicate: q => q.queryKey[2] !== account })
  }
}

/**
 * The shared store-status read. When a store's signed-in account changes
 * (a sign-in, a sign-out, a switch in the CLI, a status that needs a sign-in),
 * it drops that store's queries for every other account, list and detail.
 */
export function useMarketRegistries(): MarketRegistriesResponse | undefined {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: MARKET_REGISTRIES_QUERY_KEY,
    queryFn: () => api.getMarketRegistries(),
    staleTime: 30_000,
  })
  const accounts = JSON.stringify(Object.entries(data?.status ?? {}).map(([id, s]) => [id, statusAccount(s)]))
  useEffect(() => {
    for (const [id, account] of JSON.parse(accounts) as [string, string][]) dropOtherAccounts(queryClient, id, account)
  }, [accounts, queryClient])
  return data
}

/** Re-read the store statuses when an answer's status disagrees with the one shown. */
function useStatusAgreement(answer: { status: MarketRegistryStatus } | undefined, current: MarketRegistryStatus | undefined) {
  const queryClient = useQueryClient()
  useEffect(() => {
    if (answer && current && (answer.status.state !== current.state || statusAccount(answer.status) !== statusAccount(current))) {
      void queryClient.invalidateQueries({ queryKey: MARKET_REGISTRIES_QUERY_KEY })
    }
  }, [answer]) // eslint-disable-line react-hooks/exhaustive-deps
}

/** The detail route of one store listing. `-/` keeps it off every app name. */
export function marketListingPath(row: Pick<MarketListingRow, 'registry' | 'ref'>): string {
  const [namespace, name] = row.ref.split('/')
  return `/apps/-/market/${encodeURIComponent(row.registry)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
}

/** The read-error copy for a failed store read, by its code. */
export function readErrorKey(code: string): string {
  if (code === 'market_signed_out') return 'components.appstore.marketStore.err_signed_out'
  return code === 'market_api_refused'
    ? 'components.appstore.marketStore.err_read_refused'
    : 'components.appstore.marketStore.err_unreachable'
}

/**
 * Whether an answer may be shown: it was read for the account the current
 * store status names, and both say `signed_in`. So an answer read for one
 * account never shows, or offers Install, once another is signed in or after
 * a sign-out, whichever answer arrives last.
 */
export function answerIsCurrent(
  data: Pick<MarketListingsResponse | MarketListingResponse, 'status' | 'account'> | undefined,
  current: MarketRegistryStatus | undefined,
): boolean {
  return !!data && current?.state === 'signed_in' && data.status.state === 'signed_in'
    && data.account === current.account && data.status.account === current.account
}

/** The rows of one listing answer the page may show (see `answerIsCurrent`). */
export function signedInRows(data: MarketListingsResponse | undefined, current: MarketRegistryStatus | undefined): MarketListingRow[] {
  return data && answerIsCurrent(data, current) ? data.rows : []
}

export function MarketRow({ row, label, onInstall }: {
  row: MarketListingRow
  label: string
  onInstall: (row: MarketListingRow) => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const rating = row.rating.status === 'ok' ? row.rating.value : null
  const reviews = row.ratingCount.status === 'ok' ? row.ratingCount.value : 0
  return (
    <Clickable
      aria-label={t('components.appstore.appListRow.view_details_for', { name: row.displayName })}
      className="flex items-center gap-3.5 px-3.5 py-3 border border-border rounded-xl bg-card mb-2 cursor-pointer hover:border-border-strong transition-colors focus-ring"
      onClick={() => navigate(marketListingPath(row))}
      data-testid={`market-row-${row.ref}`}
    >
      {/* Store icons need a signed-in fetch: initials until that exists. */}
      <AppIconTile name={row.name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-[14px] font-semibold text-text-strong">
          <span className="truncate">{row.displayName}</span>
          {row.official && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={t('components.appstore.marketStore.official')} />
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[12px] text-muted min-w-0">
          <span className="truncate min-w-0">{t('components.appstore.marketStore.source_label', { label, publisher: row.publisher })}</span>
          {rating !== null && (
            <span className="inline-flex items-center gap-0.5 shrink-0" title={t('components.appstore.marketStore.rating_label')}>
              <Star size={12} className="shrink-0" aria-hidden /> {fmtNumber(rating, { maximumFractionDigits: 1 })}
            </span>
          )}
          {reviews > 0 && (
            <span className="shrink-0">{t('components.appstore.marketStore.reviews', { n: fmtNumber(reviews) })}</span>
          )}
        </div>
        <div className="text-[12.5px] text-muted truncate" title={row.summary}>{row.summary}</div>
      </div>
      <div
        className="flex flex-col items-end gap-1.5 shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {row.installed ? (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted"><Check size={12} /> {t('components.appstore.appListRow.installed')}</span>
        ) : row.nameTaken ? (
          <Btn disabled title={t('components.appstore.marketStore.name_taken')}>{t('components.appstore.appListRow.install')}</Btn>
        ) : (
          <Btn primary className="rounded-full px-3.5 text-[12px] font-semibold" onClick={() => onInstall(row)}>{t('components.appstore.appListRow.install')}</Btn>
        )}
        {row.nameTaken && !row.installed && <span className="text-[12px] text-muted">{t('components.appstore.marketStore.name_taken')}</span>}
      </div>
    </Clickable>
  )
}

export default function MarketListingRows({ offer, query = '', category = 'all', installedOnly = false }: {
  offer: MarketRegistryOffer
  query?: string
  category?: string
  installedOnly?: boolean
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const statusText = useMarketStatusText()
  const [installing, setInstalling] = useState<MarketListingRow | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const registries = useMarketRegistries()
  const current = registries?.status[offer.id]
  const key = marketListingsKey(offer.id, statusAccount(current))
  const { data, error } = useQuery({
    queryKey: key,
    queryFn: () => api.listMarketListings(offer.id),
    staleTime: 5 * 60_000,
    enabled: registries !== undefined,
  })
  useStatusAgreement(data, current)

  const refresh = async () => {
    setRefreshing(true)
    setRefreshError('')
    const before = queryClient.getQueryState(key)?.dataUpdatedAt
    try {
      const fresh = await api.listMarketListings(offer.id, true)
      // Dropped (the account changed) or replaced while this read ran: a late
      // answer never puts older rows back.
      const now = queryClient.getQueryState(key)
      if (now && now.dataUpdatedAt === before) queryClient.setQueryData<MarketListingsResponse>(key, fresh)
    } catch (e) {
      setRefreshError((e as Error)?.message || t('components.appstore.marketStore.action_failed'))
    } finally {
      setRefreshing(false)
    }
  }

  const listed = signedInRows(data, current)
  const rows = listed.filter(r => matchesMarketQuery(r, query)
    && (category === 'all' || categoryFor(marketRowTags(r)) === category)
    && (!installedOnly || r.installed))
  const status = data?.status
  // Only these two need a sign-in. `unknown` (the store or its CLI cannot be
  // read right now) shows the read error, or the status line when the status
  // names why (the CLI is missing or too old), since a retry cannot help then.
  const signedOut = status?.state === 'needs_sign_in' || status?.state === 'no_access'
  const unknownState = status?.state === 'unknown'

  return (
    <section className="mb-6" data-testid={`market-section-${offer.id}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[17px] font-semibold text-text-strong">{offer.label}</h3>
        <IconButton
          aria-label={t('components.appstore.marketStore.refresh')}
          title={t('components.appstore.marketStore.refresh')}
          onClick={() => { void refresh() }}
          disabled={refreshing}
        >
          <RefreshCw size={14} />
        </IconButton>
      </div>
      {error && <ErrorNotice message={(error as Error).message} askAgent />}
      {refreshError && <ErrorNotice message={refreshError} onDismiss={() => setRefreshError('')} askAgent />}
      {data?.error && (
        <ErrorNotice message={unknownState && status?.detail ? statusText(status) : t(readErrorKey(data.error.code))} askAgent />
      )}
      {signedOut && (
        <div className="border border-border rounded-xl bg-card px-3.5 py-3 mb-2" data-testid="market-signin-card">
          <div className="text-[14px] font-semibold text-text-strong">{t('components.appstore.marketStore.sign_in_card_title', { label: offer.label })}</div>
          <p className="text-[12.5px] text-muted">{statusText(status)}</p>
          <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.sign_in_card_body')}</p>
        </div>
      )}
      {!signedOut && !unknownState && data && !data.error && listed.length === 0 && (
        <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.empty')}</p>
      )}
      {listed.length > 0 && rows.length === 0 && (
        <p className="text-[12.5px] text-muted">{t('pages.appsPage.no_matching_apps')}</p>
      )}
      {rows.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-3.5">
          {rows.map(row => (
            <MarketRow
              key={`space:${row.registry}:${row.ref}:${cardDataKey(row)}`}
              row={row}
              label={offer.label}
              onInstall={setInstalling}
            />
          ))}
        </div>
      )}
      {data?.truncated && (
        <p className="text-[12px] text-muted mt-1" data-testid="market-truncated">
          {t('components.appstore.marketStore.truncated', { limit: fmtNumber(MARKET_LISTING_LIMIT) })}
        </p>
      )}
      {installing && (
        <InstallFromSpaceDialog
          row={installing}
          onClose={() => setInstalling(null)}
          onInstalled={() => {
            setInstalling(null)
            void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
            window.dispatchEvent(new Event('mc:apps-changed'))
          }}
        />
      )}
    </section>
  )
}

/** What the rail's category matcher reads for a store row: its category, then its tags. */
export function marketRowTags(row: Pick<MarketListingRow, 'category' | 'tags'>): string[] {
  return [row.category, ...row.tags]
}

/** The search box's match for a store row, on the same fields the block filters by. */
export function matchesMarketQuery(row: MarketListingRow, query: string): boolean {
  const needle = query.trim().toLowerCase()
  return !needle
    || row.displayName.toLowerCase().includes(needle)
    || row.name.includes(needle)
    || row.summary.toLowerCase().includes(needle)
}

/** The rail key of one Kiro Space store. A git registry name cannot hold `:`. */
export function marketSourceKey(id: string): string {
  return sourceRowKey({ name: `space:${id}`, builtin: false })
}

export function isMarketSourceKey(key: string | null): boolean {
  return !!key && key.startsWith(marketSourceKey(''))
}

/** The added stores this build offers, with their statuses, from the shared registries query. */
function useAddedStores(): { stores: MarketRegistryOffer[]; statuses: Record<string, MarketRegistryStatus> } {
  const data = useMarketRegistries()
  return useMemo(() => {
    const added = new Set(data?.added ?? [])
    return { stores: (data?.offers ?? []).filter(o => added.has(o.id)), statuses: data?.status ?? {} }
  }, [data])
}

/** One rail source row per added store, counted from its listing read under the search box. */
export function useMarketSourceRows(query = ''): SourceRow[] {
  const { stores, statuses } = useAddedStores()
  // A string, so the rows below keep one identity until a count really changes.
  const counts = useQueries({
    queries: stores.map(offer => ({
      queryKey: marketListingsKey(offer.id, statusAccount(statuses[offer.id])),
      queryFn: () => api.listMarketListings(offer.id),
      staleTime: 5 * 60_000,
    })),
    combine: results => results
      .map((r, i) => signedInRows(r.data, statuses[stores[i].id]).filter(row => matchesMarketQuery(row, query)).length)
      .join(','),
  })
  return useMemo(() => {
    const each = counts.split(',')
    return stores.map((offer, i) => ({
      name: `space:${offer.id}`, label: offer.label, count: Number(each[i]) || 0, builtin: false,
    }))
  }, [stores, counts])
}

/**
 * The store rows the rail counts, under the search box: every added store's
 * rows for All sources, the picked store's rows for a store source, and none
 * for a git source (git rows are counted by the page itself).
 */
export function useRailStoreRows(source: string | null, query = ''): MarketListingRow[] {
  const { stores, statuses } = useAddedStores()
  const counted = useMemo(() => (source === null
    ? stores
    : stores.filter(o => marketSourceKey(o.id) === source)), [stores, source])
  const all = useQueries({
    queries: counted.map(offer => ({
      queryKey: marketListingsKey(offer.id, statusAccount(statuses[offer.id])),
      queryFn: () => api.listMarketListings(offer.id),
      staleTime: 5 * 60_000,
    })),
    // Same rule as the block: rows count only from a current answer.
    combine: results => results.flatMap((r, i) => signedInRows(r.data, statuses[counted[i].id])),
  })
  return useMemo(() => all.filter(r => matchesMarketQuery(r, query)), [all, query])
}

/**
 * Every added Kiro Space store's block, for the Discover page. Renders nothing
 * when the build offers no store or none is added. With a source picked in the
 * rail, only that store's block shows; a git source hides them all. The rail's
 * category and installed-only filters apply to the store rows too.
 */
export function MarketStoreSections({ query = '', source = null, category = 'all', installedOnly = false }: {
  query?: string
  source?: string | null
  category?: string
  installedOnly?: boolean
}) {
  const { stores } = useAddedStores()
  if (source !== null && !isMarketSourceKey(source)) return null
  const shown = stores.filter(o => source === null || marketSourceKey(o.id) === source)
  if (shown.length === 0) return null
  return <>{shown.map(offer => (
    <MarketListingRows key={offer.id} offer={offer} query={query} category={category} installedOnly={installedOnly} />
  ))}</>
}
