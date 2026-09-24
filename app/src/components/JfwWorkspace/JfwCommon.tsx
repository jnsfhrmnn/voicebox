import type { ReactNode } from 'react';
import { cn } from '@/lib/utils/cn';

/**
 * Gemeinsame Bausteine des JFW-Folge-Blocks (Bedienoberflaechen-Stuecke).
 *
 * Barrierefreiheits-Regel fuer alle Zustaende: nie nur Farbe — jeder Zustand
 * traegt Text und ein Form-/Symbolzeichen (StateChip), Bedienelemente sind
 * echte Buttons/Inputs mit sichtbarem Fokusring und Fokusreihenfolge im DOM.
 */

export const JFW_FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 focus-visible:ring-offset-2 focus-visible:ring-offset-background';

/** Zustands-Chip: Text + Symbol, nie nur Farbcodierung. */
export function StateChip({
  tone = 'neutral',
  icon = '●',
  children,
}: {
  tone?: 'neutral' | 'ok' | 'warn' | 'error' | 'active';
  icon?: string;
  children: ReactNode;
}) {
  return (
    <span
      data-testid="state-chip"
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1',
        tone === 'ok' && 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/40 dark:text-emerald-300',
        tone === 'warn' && 'bg-amber-500/10 text-amber-700 ring-amber-500/40 dark:text-amber-300',
        tone === 'error' && 'bg-red-500/10 text-red-700 ring-red-500/40 dark:text-red-300',
        tone === 'active' && 'bg-accent/10 text-accent ring-accent/50',
        tone === 'neutral' && 'bg-muted text-muted-foreground ring-border',
      )}
    >
      <span aria-hidden="true">{icon}</span>
      <span>{children}</span>
    </span>
  );
}

/** Definitionszeile (Schluessel/Wert) fuer Bestaetigungsansichten. */
export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-border/60 py-1.5 last:border-b-0">
      <dt className="text-sm text-muted-foreground shrink-0">{label}</dt>
      <dd className="text-sm text-right break-words min-w-0" data-testid={`row-${label}`}>
        {children}
      </dd>
    </div>
  );
}

/** Karten-Abschnitt mit Ueberschrift. */
export function SectionCard({
  title,
  description,
  actions,
  children,
  testId,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section
      data-testid={testId}
      className="rounded-xl border border-border bg-card p-4 shadow-sm space-y-3"
    >
      <header className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold">{title}</h3>
          {description && (
            <p className="text-sm text-muted-foreground mt-0.5">{description}</p>
          )}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

/**
 * Zustandsvorschau (QA): schaltet alle verlangten Dialog-/Ansichtszustaende
 * eines Screens um, damit sie ohne laufenden Server-Auftrag pruefbar sind.
 * Die Panels rendern dieselben Zustandskomponenten auch im Live-Betrieb.
 */
export function StatePreview<T extends string>({
  label,
  states,
  active,
  onChange,
  testId,
}: {
  label: string;
  states: readonly T[];
  active: T;
  onChange: (state: T) => void;
  testId?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      data-testid={testId ?? 'state-preview'}
      className="flex flex-wrap items-center gap-1.5"
    >
      <span className="text-xs text-muted-foreground mr-1">{label}</span>
      {states.map((state) => (
        <button
          key={state}
          type="button"
          aria-pressed={state === active}
          data-state={state}
          onClick={() => onChange(state)}
          className={cn(
            'rounded-md border px-2 py-1 text-xs transition-colors',
            JFW_FOCUS_RING,
            state === active
              ? 'border-accent bg-accent/10 text-foreground font-medium'
              : 'border-border text-muted-foreground hover:bg-muted',
          )}
        >
          {state}
        </button>
      ))}
    </div>
  );
}

/** Standardbutton des Folge-Blocks. */
export function JfwButton({
  children,
  onClick,
  variant = 'default',
  disabled,
  ariaLabel,
  testId,
  type = 'button',
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: 'default' | 'primary' | 'danger' | 'ghost';
  disabled?: boolean;
  ariaLabel?: string;
  testId?: string;
  type?: 'button' | 'submit';
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      data-testid={testId}
      className={cn(
        'rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50',
        JFW_FOCUS_RING,
        variant === 'primary' && 'bg-primary text-primary-foreground hover:bg-primary/90',
        variant === 'default' && 'border border-border bg-background hover:bg-muted',
        variant === 'danger' &&
          'border border-red-500/50 text-red-700 hover:bg-red-500/10 dark:text-red-300',
        variant === 'ghost' && 'text-muted-foreground hover:bg-muted',
      )}
    >
      {children}
    </button>
  );
}
