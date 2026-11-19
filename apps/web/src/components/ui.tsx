import type { ReactNode } from "react";

interface ProgressProps {
  value: number;
  label?: string;
  tone?: "lime" | "violet" | "danger";
}

export function Progress({ value, label, tone = "lime" }: ProgressProps) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="progress-wrap">
      <div
        className={`progress-track tone-${tone}`}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      {label ? (
        <div className="progress-meta">
          <span>{label}</span>
          <span className="mono">{pct}%</span>
        </div>
      ) : null}
    </div>
  );
}

interface SparklineProps {
  points: number[];
  height?: number;
  label?: string;
}

export function Sparkline({ points, height = 64, label }: SparklineProps) {
  const width = 320;
  const max = Math.max(1, ...points);
  const step = points.length > 1 ? width / (points.length - 1) : width;
  const coords = points.map((value, index) => {
    const x = index * step;
    const y = height - 6 - (value / max) * (height - 14);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const area = coords.length
    ? `M0,${height} L${coords.join(" L")} L${width},${height} Z`
    : `M0,${height} L${width},${height} Z`;

  return (
    <div className="spark">
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={label ?? "activity"}>
        <path d={area} className="spark-area" />
        {coords.length > 1 ? <polyline points={coords.join(" ")} className="spark-line" /> : null}
      </svg>
      {label ? <span className="spark-label">{label}</span> : null}
    </div>
  );
}

interface RingProps {
  value: number;
  caption?: string;
  size?: number;
}

export function ScoreRing({ value, caption, size = 92 }: RingProps) {
  const radius = (size - 12) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, value));
  const offset = circumference - (clamped / 100) * circumference;
  const tone = clamped >= 72 ? "hot" : clamped >= 45 ? "warm" : "cool";
  return (
    <div className={`ring ring-${tone}`} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle className="ring-track" cx={size / 2} cy={size / 2} r={radius} />
        <circle
          className="ring-value"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div className="ring-copy">
        <strong>{clamped}</strong>
        {caption ? <small>{caption}</small> : null}
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
      <div className="empty-glyph">✂</div>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function Pill({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className={`pill${tone ? ` pill-${tone}` : ""}`}>{children}</span>;
}