import { AlertTriangle } from 'lucide-react'

import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/** *day* as `YYYY-MM-DD` in the viewer's own time zone, the shape the vault stores. */
export function localDateString(day: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`
}

/** Whether a rotate-by date has passed: the day after it has begun. Both are
 *  `YYYY-MM-DD`, so they compare as strings. */
export function rotationDue(rotateBy: string, today: string): boolean {
  return today > rotateBy
}

/**
 * "Rotate by <date>" under a vault entry that carries one, and a "Rotation due"
 * chip once that date has passed. Renders nothing for an entry with no date, so
 * the row reads exactly as it did before the field existed.
 */
export function RotateByNote({ name, rotateBy }: { name: string; rotateBy?: string }) {
  if (!rotateBy) return null
  const due = rotationDue(rotateBy, localDateString(new Date()))
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted">
      <span>{i18nT('settings.secrets.rotate_by_note', { date: rotateBy })}</span>
      {due && (
        <Badge
          variant="warn"
          role="note"
          aria-label={i18nT('settings.secrets.rotation_due_aria', { name, date: rotateBy })}
        >
          <AlertTriangle className="lucide-inline" aria-hidden />
          {i18nT('settings.secrets.rotation_due')}
        </Badge>
      )}
    </div>
  )
}
