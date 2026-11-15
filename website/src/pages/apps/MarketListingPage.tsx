/**
 * MarketListingPage — one Kiro Space store listing, at
 * `/apps/-/market/:registry/:namespace/:name`.
 *
 * The `-/` prefix keeps the route off every installed app's name (`-` is not a
 * valid app name). Reads `GET /api/apps/market/listing`, uncached. The summary
 * renders as plain text; the listing's Markdown readme is not shown. Install
 * goes through InstallFromSpaceDialog, keyed by store and ref, never by name.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, Check, Star } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, ContentSkeleton } from '../../components/ui'
import AppIconTile from '../../components/appstore/AppIconTile'
import InstallFromSpaceDialog from '../../components/appstore/InstallFromSpaceDialog'
import {
  answerIsCurrent, marketListingKey, readErrorKey, statusAccount, useMarketRegistries,
} from '../../components/appstore/MarketListingRows'
import { useMarketStatusText } from '../../components/appstore/MarketRegistryManager'
import { fmtNumber } from '../../i18n/format'

export default function MarketListingPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const statusText = useMarketStatusText()
  const { registry = '', namespace = '', name = '' } = useParams()
  const ref = `${namespace}/${name}`
  const [installing, setInstalling] = useState(false)
  const registries = useMarketRegistries()
  const current = registries?.status[registry]
  const { data, error, isLoading } = useQuery({
    queryKey: marketListingKey(registry, statusAccount(current), ref),
    queryFn: () => api.getMarketListing(registry, ref),
    enabled: registries !== undefined,
  })
  // Shown, with its Install button, only when read for the account signed in now.
  const listing = answerIsCurrent(data, current) ? data?.listing ?? null : null
  // One notice per cause: a sign-in problem shows the status line alone; a
  // read error while signed in (or while the status is unknown) shows the error.
  const signedOut = data?.status.state === 'needs_sign_in' || data?.status.state === 'no_access'
  const notSignedIn = !!data && data.status.state !== 'signed_in'
  const showError = !!data?.error && !signedOut
  const showStatus = signedOut || (notSignedIn && !data?.error)

  return (
    <div className="max-w-[880px] mx-auto px-4 py-6" data-testid="market-listing-page">
      <Btn onClick={() => navigate('/apps')} className="mb-4">
        <ArrowLeft size={14} /> {t('components.appstore.marketStore.back')}
      </Btn>
      {isLoading && <ContentSkeleton rows={4} />}
      {error && <ErrorNotice message={(error as Error).message} askAgent />}
      {showError && data?.error && <ErrorNotice message={t(readErrorKey(data.error.code))} askAgent />}
      {showStatus && <ErrorNotice message={statusText(data?.status)} askAgent />}
      {data && !listing && !data.error && !notSignedIn && (
        <ErrorNotice message={t('components.appstore.marketStore.not_found')} askAgent />
      )}
      {listing && (
        <div>
          <div className="flex items-start gap-4">
            <AppIconTile name={listing.name} className="w-16 h-16" />
            <div className="flex-1 min-w-0">
              <h1 className="text-[22px] font-semibold text-text-strong">{listing.displayName}</h1>
              <div className="text-[13px] text-muted">
                {t('components.appstore.marketStore.publisher', { publisher: listing.publisher })}
                {listing.latestSemver && <> · {t('components.appstore.marketStore.version', { semver: listing.latestSemver })}</>}
              </div>
              <div className="flex items-center gap-3 text-[13px] text-muted mt-1">
                {listing.rating.status === 'ok' && (
                  <span className="inline-flex items-center gap-1" title={t('components.appstore.marketStore.rating_label')}>
                    <Star size={13} aria-hidden /> {fmtNumber(listing.rating.value, { maximumFractionDigits: 1 })}
                  </span>
                )}
                {listing.ratingCount.status === 'ok' && listing.ratingCount.value > 0 && (
                  <span>{t('components.appstore.marketStore.reviews', { n: fmtNumber(listing.ratingCount.value) })}</span>
                )}
                {listing.downloads.status === 'ok' && (
                  <span data-testid="market-downloads">{t('components.appstore.marketStore.downloads', { n: fmtNumber(listing.downloads.value) })}</span>
                )}
                {listing.downloads.status === 'unavailable' && (
                  <span data-testid="market-downloads">{t('components.appstore.marketStore.downloads_unavailable')}</span>
                )}
              </div>
            </div>
            <div className="shrink-0">
              {listing.installed ? (
                <span className="inline-flex items-center gap-1 text-[13px] text-muted"><Check size={13} /> {t('components.appstore.appListRow.installed')}</span>
              ) : (
                <Btn primary disabled={listing.nameTaken} onClick={() => setInstalling(true)} title={listing.nameTaken ? t('components.appstore.marketStore.name_taken') : undefined}>
                  {t('components.appstore.appListRow.install')}
                </Btn>
              )}
            </div>
          </div>
          {listing.nameTaken && !listing.installed && (
            <p className="text-[12.5px] text-muted mt-2">{t('components.appstore.marketStore.name_taken')}</p>
          )}
          <p className="text-[14px] mt-5 whitespace-pre-wrap">{listing.summary}</p>
          {installing && (
            <InstallFromSpaceDialog
              row={listing}
              onClose={() => setInstalling(false)}
              onInstalled={() => {
                setInstalling(false)
                void queryClient.invalidateQueries({ queryKey: ['market-listing', registry] })
                void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
                window.dispatchEvent(new Event('mc:apps-changed'))
              }}
            />
          )}
        </div>
      )}
    </div>
  )
}
