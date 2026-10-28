import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../app/api";
import { useLive } from "../app/live";
import { formatScore } from "../app/format";
import { QueryError } from "../components";

type Experiment = {
  id: string;
  name: string;
  status: string;
  scoring_version: string;
  scorer_name: string;
  execution_mode: string;
  budget: number;
  trials_told: number;
  best_objective: number | null;
  best_score_source: string | null;
  variability_penalty: number;
  created_at: string;
  updated_at: string;
};

type Page = { items: Experiment[]; next_cursor: string | null };

const RUN_LABEL: Record<string, string> = {
  DRAFT: "Not started",
  RUNNING: "Running",
  PAUSED: "Paused",
  VALIDATING: "Checking held-out sample",
  STOPPING: "Stopping",
  STOPPED: "Stopped",
  COMPLETED: "Finished",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export function ExperimentsPage() {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const query = useLive<Page>(
    ["experiments", q, status],
    `/api/v1/experiments?limit=50${q ? `&q=${encodeURIComponent(q)}` : ""}${status ? `&status=${encodeURIComponent(status)}` : ""}`,
  );
  return (
    <>
      <h1>Runs</h1>
      <div className="row">
        <label>Search<input value={q} onChange={(event) => setQ(event.target.value)} /></label>
        <label>
          Status
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">Any</option>
            {Object.entries(RUN_LABEL).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
      </div>
      {query.isPending ? <p>Loading experiments…</p> : null}
      {query.isError ? <QueryError error={query.error} /> : null}
      {query.data ? (
        <div className="panel">
          {query.data.items.length === 0 ? <p>No experiments match.</p> : (
            <table>
              <thead>
                <tr>
                  <th>Name</th><th>Status</th><th>Finished</th><th>Best score</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((row) => (
                  <tr key={row.id}>
                    <td><Link to={`/experiments/${row.id}`}>{row.name}</Link></td>
                    <td>{RUN_LABEL[row.status] ?? row.status}</td>
                    <td>{row.trials_told} of {row.budget}</td>
                    <td>{formatScore(row.best_objective, row.best_score_source)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
      <CreateForm runs={query.data?.items ?? []} />
    </>
  );
}

function runDirectory(name: string, version: string, sampleId: string): string {
  const typed = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const leaf = typed || `gatk_${version}_${sampleId.replace(/-/g, "").slice(0, 7)}`;
  return `tuning_runs/${leaf}`;
}

function CreateForm({ runs }: { runs: Experiment[] }) {
  const client = useQueryClient();
  const samples = useQuery({
    queryKey: ["samples"],
    queryFn: () => api<{ items: { sample_id: string }[] }>("/api/v1/samples"),
  });
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [trials, setTrials] = useState(400);
  const [workers, setWorkers] = useState(1);
  const [phase, setPhase] = useState<1 | 2>(2);
  const [version, setVersion] = useState<"v1" | "v2">("v2");
  const [sampleId, setSampleId] = useState("");
  const [resume, setResume] = useState(false);
  const ids = samples.data?.items.map((item) => item.sample_id) ?? [];
  const selected = sampleId || ids[0] || "";
  const directory = selected ? runDirectory(name, version, selected) : "tuning_runs/";
  const leaf = directory.split("/").pop() ?? directory;
  const mutation = useMutation({
    mutationFn: async () => {
      if (!selected) {
        throw new ApiError(400, "sample", "Choose a sample.");
      }
      const existing = runs.find((row) => row.name === leaf);
      if (resume) {
        if (!existing) {
          throw new ApiError(404, "not_found", "There is no run with this directory to resume.");
        }
        if (existing.status === "PAUSED") {
          return api(`/api/v1/experiments/${existing.id}/resume`, { method: "POST" });
        }
        return existing;
      }
      await api("/api/v1/datasets/import-sample", {
        method: "POST",
        body: JSON.stringify({ sample_id: selected, scoring_version: version }),
      });
      return api("/api/v1/experiments", {
        method: "POST",
        body: JSON.stringify({
          name: leaf,
          search_profile: phase === 2 ? "stage2" : "stage1",
          trials,
          scoring_version: version,
          training_dataset_ids: [selected],
          held_out_dataset_ids: [],
          execution_mode: "official",
          variability_penalty: 0,
          max_parallel_global: workers,
          start: true,
        }),
      });
    },
    onSuccess: async () => {
      setError(null);
      await client.invalidateQueries({ queryKey: ["experiments"] });
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Create failed"),
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }

  return (
    <form className="panel" onSubmit={onSubmit}>
      <h2>New run</h2>
      <div className="grid">
        <label>Name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="optional" /></label>
        <label>Tries<input type="number" min={1} value={trials} onChange={(event) => setTrials(Number(event.target.value))} /></label>
        <label>Workers<input type="number" min={1} value={workers} onChange={(event) => setWorkers(Number(event.target.value))} /></label>
        <label>Phase
          <select value={phase} onChange={(event) => setPhase(Number(event.target.value) === 1 ? 1 : 2)}>
            <option value={1}>1</option>
            <option value={2}>2</option>
          </select>
        </label>
        <label>Score
          <select value={version} onChange={(event) => setVersion(event.target.value as "v1" | "v2")}>
            <option value="v1">v1</option>
            <option value="v2">v2</option>
          </select>
        </label>
        <label>Sample
          <select value={selected} onChange={(event) => setSampleId(event.target.value)} required>
            {samples.isPending ? <option value="">Loading samples…</option> : null}
            {ids.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
      </div>
      <p>Run directory <code>{directory}</code></p>
      <label className="row">
        <input type="checkbox" checked={resume} onChange={(event) => setResume(event.target.checked)} />
        Resume
      </label>
      {samples.isError ? <QueryError error={samples.error} /> : null}
      {error ? <p className="error" role="alert">{error}</p> : null}
      <button type="submit" disabled={mutation.isPending || !selected}>{mutation.isPending ? "Starting…" : "Create run"}</button>
    </form>
  );
}
