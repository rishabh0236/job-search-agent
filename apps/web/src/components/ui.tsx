/**
 * Shared primitives.
 *
 * `TrustBadge` is the most important component in the app. The four categories the
 * product promises to distinguish — verified fact, AI suggestion, user-provided,
 * unknown — must be visually unmistakable everywhere they appear, so the mapping
 * lives in exactly one place rather than being re-derived per screen.
 */

import type { ReactNode } from "react";

import type { ApiError, Provenance } from "../lib/api";

// ------------------------------------------------------------------ trust

interface TrustDescriptor {
  label: string;
  className: string;
  title: string;
}

export function trustDescriptor(provenance: Provenance, verified: boolean): TrustDescriptor {
  if (provenance === "unknown") {
    return {
      label: "Unknown",
      className: "badge-unknown",
      title: "Unknown / requires confirmation — the system will not use this until you confirm it",
    };
  }
  if (provenance === "user") {
    return {
      label: "You provided",
      className: "badge-user",
      title: "User-provided information — you supplied this, it did not come from your resume",
    };
  }
  if (provenance === "ai_suggestion") {
    return {
      label: "AI suggestion",
      className: "badge-ai",
      title: "AI suggestion — proposed by the model and not yet confirmed by you",
    };
  }
  return verified
    ? {
        label: "Verified fact",
        className: "badge-verified",
        title: "Verified candidate fact — read from your resume and confirmed by you",
      }
    : {
        label: "Extracted",
        className: "badge-extracted",
        title: "Extracted from your resume but not yet confirmed",
      };
}

export function TrustBadge({
  provenance,
  verified,
}: {
  provenance: Provenance;
  verified: boolean;
}) {
  const descriptor = trustDescriptor(provenance, verified);
  return (
    <span className={`badge ${descriptor.className}`} title={descriptor.title}>
      <span className="badge-dot" aria-hidden="true" />
      {descriptor.label}
    </span>
  );
}

export function TrustLegend() {
  const entries: [Provenance, boolean][] = [
    ["resume", true],
    ["resume", false],
    ["user", true],
    ["ai_suggestion", false],
    ["unknown", false],
  ];
  return (
    <div className="row" style={{ gap: "0.4rem" }}>
      {entries.map(([provenance, verified]) => (
        <TrustBadge key={`${provenance}-${String(verified)}`} provenance={provenance} verified={verified} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ states

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <div className="stack" style={{ alignItems: "center" }}>
        <div className="skeleton" style={{ height: 10, width: 180 }} />
        <div className="skeleton" style={{ height: 10, width: 120 }} />
        <span className="subtle">{label}…</span>
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <p style={{ maxWidth: "48ch", margin: "0 auto" }}>{body}</p>
      {action ? <div className="empty-actions">{action}</div> : null}
    </div>
  );
}

/**
 * Errors are not all alike. A safety stop is a deliberate interrupt that needs an
 * explanation and a human decision, so it is rendered as such rather than as a
 * generic failure.
 */
export function ErrorBox({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  if (error.isSafetyStop || error.requiresUserAction) {
    return (
      <div className="callout callout-stop" role="alert">
        <div>
          <strong>Stopped for you to decide{error.reason ? `: ${error.reason.replace(/_/g, " ")}` : ""}</strong>
          {error.message}
          <p className="subtle" style={{ marginTop: "0.4rem" }}>
            Nothing was submitted. Handle this yourself in the browser, then continue.
          </p>
        </div>
      </div>
    );
  }

  const blockers = error.details.blockers;
  return (
    <div className="error-box" role="alert">
      <h3>{error.code === "network_error" ? "Cannot reach the API" : "Something went wrong"}</h3>
      <p>{error.message}</p>
      {Array.isArray(blockers) && blockers.length > 0 ? (
        <ul>
          {blockers.map((item) => (
            <li key={String(item)}>{String(item)}</li>
          ))}
        </ul>
      ) : null}
      {onRetry ? (
        <button className="btn-sm" onClick={onRetry} style={{ marginTop: "0.6rem" }}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ display

export function Card({
  title,
  actions,
  children,
  badge,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card">
      {title || actions ? (
        <div className="card-head" style={{ marginBottom: "0.7rem" }}>
          <div className="card-title">
            {typeof title === "string" ? <h2>{title}</h2> : title}
            {badge}
          </div>
          {actions ? <div className="row">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: string | number;
  note?: string;
  tone?: "ok" | "warn" | "danger";
}) {
  const color =
    tone === "danger" ? "var(--danger)" : tone === "warn" ? "var(--warn)" : tone === "ok" ? "var(--ok)" : undefined;
  return (
    <div className="stat" style={color ? { borderLeftColor: color } : undefined}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={color ? { color } : undefined}>
        {value}
      </div>
      {note ? <div className="stat-note">{note}</div> : null}
    </div>
  );
}

export function ScoreMeter({ score }: { score: number }) {
  const percentage = Math.round(score * 100);
  const tone = percentage >= 70 ? "var(--ok)" : percentage >= 45 ? "var(--warn)" : "var(--danger)";
  return (
    <div style={{ minWidth: 132 }}>
      <div className="between" style={{ marginBottom: 3 }}>
        <span className="subtle">Match</span>
        <span className="score" style={{ color: tone }}>
          {percentage}%
        </span>
      </div>
      <div className="meter">
        <div className="meter-fill" style={{ width: `${percentage}%`, background: tone }} />
      </div>
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  created: "badge-neutral",
  preparing: "badge-neutral",
  ready_for_review: "badge-warn",
  user_approved: "badge-ok",
  submitting: "badge-warn",
  submitted: "badge-ok",
  verification_required: "badge-danger",
  failed: "badge-danger",
  stopped: "badge-danger",
  eligible: "badge-ok",
  ineligible: "badge-danger",
  unknown: "badge-warn",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`badge ${STATUS_TONE[status] ?? "badge-neutral"}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function Evidence({ quote, locator }: { quote: string; locator: string }) {
  return (
    <div className="evidence">
      “{quote}”
      <span className="mono" style={{ marginLeft: "0.4rem" }}>
        {locator}
      </span>
    </div>
  );
}

export function CapabilityDot({ capability }: { capability: { name: string; status: string; detail: string } }) {
  const tone =
    capability.status === "ok" ? "badge-ok" : capability.status === "degraded" ? "badge-warn" : "badge-danger";
  return (
    <span className={`badge ${tone}`} title={capability.detail}>
      <span className="badge-dot" aria-hidden="true" />
      {capability.name}
    </span>
  );
}
