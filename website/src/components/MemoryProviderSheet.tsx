import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import { MEMORY_MOVE_ACTIVE_STATES, type MemoryProviderOverview, type MemoryProviderRow } from '../api/client/memory'
import {
  registerMemoryProviderSheetHost,
  type MemoryProviderChangeOutcome,
  type MemoryProviderChangeTarget,
} from '../app-sdk/memoryProviderChange'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { AckBox, HostNote, QuietBtn, SheetActions, SheetCard, SheetRow } from './memorySheetParts'
import { Btn, Checkbox } from './ui'
import { i18nT } from '../i18n/t'

/** The React Query key every memory provider surface reads and refreshes. */
export const MEMORY_PROVIDER_QUERY_KEY = ['memory-provider'] as const

/** The provider a target names, from the overview. */
export function providerRow(
  overview: MemoryProviderOverview | undefined,
  id: string | undefined,
): MemoryProviderRow | undefined {
  return id ? overview?.providers.find(p => p.id === id) : undefined
}

/** The display name of whichever memory *id* names; an unknown provider shows its own id. */
export function providerName(overview: MemoryProviderOverview | undefined, id: string | undefined): string {
  const row = providerRow(overview, id)
  if (row) return row.display_name
  return !id || id === 'builtin' ? i18nT('pages.overview.memoryProvider.builtin_name') : id
}

/** The sentence for a refused switch, by the gateway's code. */
function refusal(e: unknown): string {
  switch (apiErrorCode(e)) {
    case 'replace_unsupported':
      return i18nT('pages.overview.memoryProvider.error_replace_unsupported')
    case 'augment_unsupported':
      return i18nT('pages.overview.memoryProvider.error_augment_unsupported')
    case 'egress_consent_required':
      return i18nT('pages.overview.memoryProvider.error_egress_consent')
    case 'stop_learning_ack_required':
      return i18nT('pages.overview.memoryProvider.error_stop_learning')
    case 'member_stores_must_stay_builtin':
      return i18nT('pages.overview.memoryProvider.error_member_stores')
    case 'shared_crew':
      return i18nT('pages.overview.memoryProvider.error_shared_crew')
    case 'policy_denied':
      return i18nT('pages.overview.memoryProvider.error_policy')
    case 'owner_only':
      return i18nT('pages.overview.memoryProvider.error_owner_only')
    default:
      return i18nT('pages.overview.memoryProvider.error_switch_failed')
  }
}

/**
 * The confirmation sheet for one switch. The owner's press is the switch: it calls
 * `PUT /api/memory/provider`, which refuses every app token, then optionally moves
 * memory into the new provider as a move job (nothing is deleted on either side).
 */
export function MemoryProviderSheet({
  target,
  onDone,
}: {
  target: MemoryProviderChangeTarget
  onDone: (outcome: MemoryProviderChangeOutcome) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const overview = useQuery({ queryKey: MEMORY_PROVIDER_QUERY_KEY, queryFn: () => api.memoryProvider() })
  const data = overview.data
  const row = providerRow(data, target.id)
  const caps = row?.capabilities
  const needsEgress = !!caps && caps.egress === 'remote' && !row?.egress_consented
  const needsStopAck = target.mode === 'replace' && caps?.ingest === 'none'
  const needsPin = !!caps && !caps.isolation.multi_store
  const leavingReplace = target.mode === 'builtin' && data?.mode === 'replace' && !!data.id
  const liveFrom = target.mode === 'replace' || leavingReplace
    ? data?.mode === 'replace' ? data.id : 'builtin'
    : ''
  const liveEnd = target.mode === 'replace' ? target.id ?? '' : leavingReplace ? 'builtin' : ''
  // Re-confirming the provider already in use copies nothing: a copy needs two ends.
  const liveTo = liveEnd === liveFrom ? '' : liveEnd
  // Frozen when the owner confirms: the switch refreshes the overview, and the copy's
  // two ends (and its "Copy again") must not follow the new active mode.
  const [frozen, setFrozen] = useState<{ from: string; to: string; fromName: string } | null>(null)
  const copyFrom = frozen?.from ?? liveFrom
  const copyTo = frozen?.to ?? liveTo
  const [frozenView, setFrozenView] = useState<{ stopping: boolean; leaving: string; leavingRemote: boolean } | null>(null)
  const [egress, setEgress] = useState(false)
  const [stopAck, setStopAck] = useState(false)
  const [pin, setPin] = useState(false)
  const [copy, setCopy] = useState(true)
  // The switch is done once the PUT answers. A copy then runs as a job the sheet
  // follows to its end; a copy that fails is reported on its own, so the sheet never
  // calls a switch that happened a failure, and closing it still answers 'switched'.
  const [copyJobId, setCopyJobId] = useState('')
  const [copyStartFailed, setCopyStartFailed] = useState(false)
  const switchMut = useMutation({
    mutationFn: async (): Promise<string | null> => {
      setFrozen({ from: liveFrom, to: liveTo, fromName: providerName(data, liveFrom) })
      setFrozenView(liveView)
      await api.switchMemoryProvider({
        mode: target.mode,
        ...(target.id ? { id: target.id } : {}),
        ...(needsEgress ? { egress_consent: egress } : {}),
        ...(needsStopAck ? { stop_learning_ack: stopAck } : {}),
        ...(needsPin ? { pin_member_stores_builtin: pin } : {}),
      })
      if (!copy || !copyFrom || !copyTo) return ''
      try {
        return (await api.startMemoryMove({ from: copyFrom, to: copyTo })).id
      } catch {
        return null
      }
    },
    onSuccess: jobId => {
      void queryClient.invalidateQueries({ queryKey: MEMORY_PROVIDER_QUERY_KEY })
      if (jobId === '') onDone('switched')
      else if (jobId === null) setCopyStartFailed(true)
      else setCopyJobId(jobId)
    },
  })
  const retryMut = useMutation({
    mutationFn: () => api.startMemoryMove({ from: copyFrom, to: copyTo }),
    onSuccess: started => {
      setCopyStartFailed(false)
      setCopyJobId(started.id)
    },
    onError: () => setCopyStartFailed(true),
  })
  const copyJob = useQuery({
    queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'copy', copyJobId],
    queryFn: () => api.memoryMove(copyJobId),
    enabled: !!copyJobId,
    refetchInterval: query =>
      query.state.data && !MEMORY_MOVE_ACTIVE_STATES.has(query.state.data.state) ? false : 1000,
  })
  const job = copyJob.data
  // A refused, erroring or conflicting item (edited on both sides since an earlier copy)
  // was not copied; the owner is told, never a silent close.
  const copyErrors = job
    ? Object.entries(job.outcomes).some(([k, n]) => n > 0 && (k.startsWith('error:') || k.startsWith('refused') || k.startsWith('conflict:')))
    : false
  const copyPartial = job?.state === 'done' && copyErrors
  const copyFailed =
    copyStartFailed ||
    copyJob.isError ||
    job?.state === 'failed' ||
    job?.state === 'paused' ||
    job?.state === 'cancelled' ||
    (job?.state === 'done' && copyErrors)
  const copyRunning = !!copyJobId && !copyFailed && (!job || MEMORY_MOVE_ACTIVE_STATES.has(job.state))
  useEffect(() => {
    if (job?.state === 'done' && !copyErrors) onDone('switched')
  }, [job?.state, copyErrors, onDone])
  const switched = copyStartFailed || !!copyJobId
  const close = () => onDone(switched ? 'switched' : 'cancelled')
  const name = providerName(data, target.id)
  const leavingName = providerName(data, data?.mode === 'builtin' ? undefined : data?.id)
  const blocked = (needsEgress && !egress) || (needsStopAck && !stopAck) || (needsPin && !pin)
  // What the sheet says depends on the mode it was opened from. The switch refreshes
  // the overview, so the words are frozen on confirm to keep naming what was approved.
  const liveView = {
    stopping: target.mode === 'builtin' && data?.mode === 'augment',
    leaving: leavingName,
    leavingRemote: providerRow(data, data?.id)?.capabilities?.egress === 'remote',
  }
  const view = frozenView ?? liveView
  const title =
    view.stopping
      ? t('pages.overview.memoryProvider.sheet_title_stop', { name: view.leaving })
      : target.mode === 'augment'
        ? t('pages.overview.memoryProvider.sheet_title_add', { name })
        : t('pages.overview.memoryProvider.sheet_title_switch', {
          name: target.mode === 'builtin' ? t('pages.overview.memoryProvider.builtin_name') : name,
        })
  const lead = view.stopping
    ? t('pages.overview.memoryProvider.sheet_body_stop_augment', { name: view.leaving })
    : target.mode === 'builtin'
      ? t('pages.overview.memoryProvider.sheet_body_builtin', { name: view.leaving })
      : target.mode === 'replace'
        ? t('pages.overview.memoryProvider.lead_replace')
        : t('pages.overview.memoryProvider.lead_augment')
  // Crew memory answers every row the same way; a provider's rows come from its handshake.
  // Stopping a source describes that source, the subject the Memory row names, and a
  // source is only ever read.
  const toProvider = target.mode !== 'builtin'
  const remote = view.stopping ? view.leavingRemote : toProvider && caps?.egress === 'remote'
  const readOnly = view.stopping || target.mode === 'augment' || (toProvider && !!caps?.read_only)
  const canCopy = !!copyFrom && !!copyTo
  const copySourceName = frozen?.fromName ?? providerName(data, copyFrom)
  const existingAnswer = canCopy
    ? copy
      ? t('pages.overview.memoryProvider.existing_copied', { name: copySourceName })
      : t('pages.overview.memoryProvider.existing_not_copied_from', { name: copySourceName })
    : t('pages.overview.memoryProvider.existing_not_copied')
  return (
    <Modal
      open
      onClose={close}
      title={title}
      maxWidth={440}
      layer="top"
      dismissDisabled={switchMut.isPending}
      footer={
        switched ? (
          <SheetActions
            primary={
              <Btn primary className="w-full justify-center py-2" onClick={close}>
                {t('pages.overview.memoryProvider.close')}
              </Btn>
            }
            quiet={
              // A copy that failed after the switch starts again here, the same job.
              copyFailed && canCopy ? (
                <QuietBtn busy={retryMut.isPending} onClick={() => retryMut.mutate()}>
                  {t('pages.overview.memoryProvider.copy_again')}
                </QuietBtn>
              ) : null
            }
          />
        ) : (
          <SheetActions
            primary={
              <Btn
                primary
                className="w-full justify-center py-2"
                busy={switchMut.isPending}
                disabled={blocked || !overview.isSuccess}
                onClick={() => switchMut.mutate()}
              >
                {view.stopping
                  ? t('pages.overview.memoryProvider.confirm_stop')
                  : target.mode === 'augment'
                    ? t('pages.overview.memoryProvider.add_source')
                    : t('pages.overview.memoryProvider.confirm')}
              </Btn>
            }
            quiet={
              <QuietBtn className="text-muted" onClick={close} disabled={switchMut.isPending}>
                {t('pages.overview.memoryProvider.cancel')}
              </QuietBtn>
            }
          />
        )
      }
    >
      <div className="flex flex-col gap-3 text-sm" data-testid="memory-provider-sheet">
        <p className="text-text">{lead}</p>
        <SheetCard testId="memory-provider-rows">
          {/* The title can be cut short on a narrow window; this row always names what
              the owner approves. */}
          <SheetRow label={t('pages.overview.memoryProvider.row_memory')}>
            {view.stopping
              ? view.leaving
              : target.mode === 'builtin'
                ? t('pages.overview.memoryProvider.builtin_name')
                : name}
          </SheetRow>
          <SheetRow label={t('pages.overview.memoryProvider.row_where')} warn={remote}>
            {remote ? t('pages.overview.memoryProvider.where_remote') : t('pages.overview.memoryProvider.where_local')}
          </SheetRow>
          <SheetRow label={t('pages.overview.memoryProvider.row_access')}>
            {readOnly ? t('pages.overview.memoryProvider.access_read_only') : t('pages.overview.memoryProvider.access_all')}
          </SheetRow>
          <SheetRow label={t('pages.overview.memoryProvider.row_existing')}>
            {existingAnswer}
          </SheetRow>
          {target.mode === 'replace' && (
            <SheetRow label={t('pages.overview.memoryProvider.row_switch_back')}>
              {t('pages.overview.memoryProvider.switch_back_any')}
            </SheetRow>
          )}
        </SheetCard>
        {/* The one choice on the card, kept out of the read-only rows so it reads as one,
            and naming both ends so the owner sees which way the copy goes. */}
        {canCopy && !switched && (
          <label className="flex items-start gap-2 rounded-lg border border-border px-3 py-2.5">
            <Checkbox className="mt-[3px] shrink-0" checked={copy} onChange={e => setCopy(e.target.checked)} />
            <span className="text-text-strong">
              {t('pages.overview.memoryProvider.copy_first_both', {
                from: copySourceName,
                to: providerName(data, copyTo === 'builtin' ? undefined : copyTo),
              })}
            </span>
          </label>
        )}
        {(needsEgress || needsStopAck || needsPin) && (
          <AckBox>
            {needsEgress && (
              <label className="flex items-start gap-2">
                <Checkbox className="mt-[3px] shrink-0" checked={egress} onChange={e => setEgress(e.target.checked)} />
                <span>{t('pages.overview.memoryProvider.egress_consent', { name })}</span>
              </label>
            )}
            {needsStopAck && (
              <label className="flex items-start gap-2">
                <Checkbox className="mt-[3px] shrink-0" checked={stopAck} onChange={e => setStopAck(e.target.checked)} />
                <span>{t('pages.overview.memoryProvider.stop_learning_ack', { name })}</span>
              </label>
            )}
            {needsPin && (
              <label className="flex items-start gap-2">
                <Checkbox className="mt-[3px] shrink-0" checked={pin} onChange={e => setPin(e.target.checked)} />
                <span>{t('pages.overview.memoryProvider.pin_member_stores', { name })}</span>
              </label>
            )}
          </AckBox>
        )}
        {/* No hand-off: the sheet holds the owner's unsent consent choices, which need this read. */}
        {overview.isError && (
          <ErrorNotice variant="inline" message={t('pages.overview.memoryProvider.load_failed')} />
        )}
        {/* No hand-off: the sheet holds the owner's unsent consent choices. */}
        {switchMut.isError && <ErrorNotice variant="inline" message={refusal(switchMut.error)} />}
        {copyRunning && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryProvider.copy_running')}
          </p>
        )}
        {/* The switch is saved by now and no choice is left unsent, so the agent can pick
            up a failed copy. */}
        {copyFailed && (
          <ErrorNotice
            variant="inline"
            askAgent
            onHandoff={close}
            message={t(copyPartial ? 'pages.overview.memoryProvider.copy_partial' : 'pages.overview.memoryProvider.copy_failed')}
          />
        )}
        <HostNote>
          {view.stopping
            ? t('pages.overview.memoryProvider.host_note_stop')
            : target.mode === 'augment'
              ? t('pages.overview.memoryProvider.host_note_add')
              : t('pages.overview.memoryProvider.host_note')}
        </HostNote>
      </div>
    </Modal>
  )
}

/**
 * Mounts the sheet `requestMemoryProviderChange()` opens. One per window, beside
 * the other host dialogs; an ask that arrives while one is open replaces nothing
 * and answers `'cancelled'`.
 */
export function MemoryProviderSheetHost() {
  const [target, setTarget] = useState<MemoryProviderChangeTarget | null>(null)
  const settle = useRef<((outcome: MemoryProviderChangeOutcome) => void) | null>(null)
  useEffect(() => {
    const off = registerMemoryProviderSheetHost(next => new Promise(resolve => {
      if (settle.current) {
        resolve('cancelled')
        return
      }
      settle.current = resolve
      setTarget(next)
    }))
    return () => {
      off()
      settle.current?.('cancelled')
      settle.current = null
    }
  }, [])
  if (!target) return null
  return (
    <MemoryProviderSheet
      target={target}
      onDone={outcome => {
        settle.current?.(outcome)
        settle.current = null
        setTarget(null)
      }}
    />
  )
}
