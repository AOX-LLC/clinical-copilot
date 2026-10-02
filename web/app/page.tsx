import { getApiStatus, type ApiStatus } from "@/lib/api-status";

export const dynamic = "force-dynamic";

const STATUS_LABELS: Record<ApiStatus["state"], { mark: string; text: string }> = {
  ready: { mark: "✓", text: "API ready" },
  "not-ready": { mark: "!", text: "API not ready" },
  unreachable: { mark: "×", text: "API unreachable" },
};

export default async function HomePage() {
  const status = await getApiStatus();
  const label = STATUS_LABELS[status.state];

  return (
    <>
      <header className="site-header">
        <h1>Clinical Copilot</h1>
      </header>
      <main className="site-main">
        <p role="note" className="notice">
          Synthetic data only. No real patient information is used or accepted.
        </p>
        <p className="lede">
          A physician-facing view of each patient&rsquo;s timeline, lab trends and
          pre-visit summaries, where every summary line cites its source record and
          reaches the chart only after the physician approves it.
        </p>
        <p className="status" data-state={status.state}>
          <span className="status-label">API status</span>
          <span className="status-value">
            <span className="status-mark" aria-hidden="true">
              {label.mark}
            </span>
            {label.text}
          </span>
        </p>
      </main>
    </>
  );
}
