import { useEffect, useMemo, useState } from "react"

type JsonlRecord = {
  run_name?: string
  event?: any
}

function parseJsonl(text: string): any[] {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean)
  return lines.map((l) => JSON.parse(l))
}

export default function App() {
  const [run, setRun] = useState<string>("tau2_retail_03-17_07-09")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const urls = useMemo(() => {
    return {
      pareto: `/visualizer_dump/pareto_front_updated.jsonl`,
      accepted: `/visualizer_dump/candidate_accepted.jsonl`,
      rejected: `/visualizer_dump/candidate_rejected.jsonl`,
      valset: `/visualizer_dump/valset_evaluated.jsonl`,
    }
  }, [])

  const [acceptedCount, setAcceptedCount] = useState<number | null>(null)
  const [rejectedCount, setRejectedCount] = useState<number | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        setError(null)

        // At first we assume a single run is embedded under /visualizer_dump/.
        // Next iteration will add run selection via a manifest.
        const accepted = await fetch(urls.accepted).then((r) => r.text())
        const rejected = await fetch(urls.rejected).then((r) => r.text())
        setAcceptedCount(parseJsonl(accepted).length)
        setRejectedCount(parseJsonl(rejected).length)
      } catch (e: any) {
        setError(e?.message ?? String(e))
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [urls])

  return (
    <div style={{ fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Arial", padding: 24 }}>
      <h1 style={{ marginTop: 0 }}>GEPA Visualizer (dump viewer)</h1>

      <div style={{ marginBottom: 12 }}>
        <strong>Run:</strong> <code>{run}</code>
      </div>

      {loading && <p>Loading JSONL dump...</p>}
      {error && (
        <p style={{ color: "crimson" }}>
          Error: {error}
        </p>
      )}

      <h2>Summary</h2>
      <ul>
        <li>Accepted records: {acceptedCount ?? "—"}</li>
        <li>Rejected records: {rejectedCount ?? "—"}</li>
      </ul>

      <p style={{ color: "#555" }}>
        This is a scaffold. Next we’ll add:
        (1) per-iteration step view,
        (2) candidate diff vs parent,
        (3) per-task trace + litellm diagnosis display.
      </p>
    </div>
  )
}

