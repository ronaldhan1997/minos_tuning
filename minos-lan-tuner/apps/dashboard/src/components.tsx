import { useState } from "react";
import { ApiError } from "./app/api";
import { formatScore, formatWhen } from "./app/format";

export function QueryError({ error }: { error: unknown }) {
  const message = error instanceof ApiError ? error.message : "The coordinator did not answer.";
  return <p className="error" role="alert">{message}</p>;
}

export function When({ value }: { value: string | null | undefined }) {
  const formatted = formatWhen(value);
  return <time dateTime={formatted.utc} title={formatted.utc}>{formatted.local}</time>;
}

export function Score({ value, source }: { value: number | null | undefined; source?: string | null }) {
  return <span title={source === "mock" ? "Not an AdvancedScorer result" : source ?? ""}>{formatScore(value, source)}</span>;
}

export function Usage({ label, percent, detail }: { label: string; percent: number | null; detail: string }) {
  const width = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  return (
    <div className="usage">
      <div>{detail}</div>
      <div className="meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}>
        <span style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

export function Badge({ kind, children }: { kind: "ok" | "warn" | "bad" | "plain"; children: string }) {
  return <span className={`badge ${kind === "plain" ? "" : kind}`}>{children}</span>;
}

export function presenceKind(presence: string): "ok" | "warn" | "bad" | "plain" {
  if (presence === "online-idle" || presence === "online-busy") return "ok";
  if (presence === "paused" || presence === "draining") return "warn";
  if (presence === "offline" || presence === "incompatible" || presence === "unknown") return "bad";
  return "plain";
}

export function ConfirmButton({
  label,
  title,
  body,
  confirmLabel,
  onConfirm,
  disabled,
  tone = "primary",
}: {
  label: string;
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
  tone?: "primary" | "danger" | "secondary";
}) {
  const [open, setOpen] = useState(false);
  const className = tone === "danger" ? "danger" : tone === "secondary" ? "secondary" : undefined;
  return (
    <>
      <button type="button" className={`mutate ${className ?? ""}`} disabled={disabled} onClick={() => setOpen(true)}>
        {label}
      </button>
      {open ? (
        <dialog open aria-labelledby="confirm-title">
          <h2 id="confirm-title">{title}</h2>
          <p>{body}</p>
          <div className="row">
            <button
              type="button"
              className={className}
              onClick={() => {
                setOpen(false);
                onConfirm();
              }}
            >
              {confirmLabel}
            </button>
            <button type="button" className="secondary" onClick={() => setOpen(false)}>
              Back
            </button>
          </div>
        </dialog>
      ) : null}
    </>
  );
}

export function LineChart({
  points,
  label,
}: {
  points: { x: string; y: number }[];
  label: string;
}) {
  if (points.length === 0) {
    return <p className="muted">No scored trials yet. Missing scores are not drawn as zero.</p>;
  }
  const values = points.map((point) => point.y);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 640;
  const height = 200;
  const pad = 28;
  const coords = points.map((point, index) => {
    const x = pad + (index / Math.max(points.length - 1, 1)) * (width - pad * 2);
    const y = height - pad - ((point.y - min) / span) * (height - pad * 2);
    return `${x},${y}`;
  });
  const last = points[points.length - 1];
  return (
    <figure>
      <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
        <line x1={pad} y1={height - pad} x2={width - pad} y2={height - pad} stroke="#ddd4c6" />
        <line x1={pad} y1={pad} x2={pad} y2={height - pad} stroke="#ddd4c6" />
        <text x={pad} y={16} fontSize="12" fill="#5e584e">{max.toFixed(2)}</text>
        <text x={pad} y={height - 8} fontSize="12" fill="#5e584e">{min.toFixed(2)}</text>
        <polyline fill="none" stroke="#1b4332" strokeWidth="2" points={coords.join(" ")} />
      </svg>
      <figcaption className="muted">
        {label} Latest value {last.y.toFixed(3)} after {points.length} scored trials.
      </figcaption>
    </figure>
  );
}
