import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../app/api";
import { formatDuration } from "../app/format";
import { useLive } from "../app/live";
import { QueryError, Score, When } from "../components";

type Job = {
  job_id: string;
  experiment_id: string;
  trial_id: string;
  dataset_id: string;
  role: string;
  state: string;
  attempt: number;
  worker_id: string | null;
  official_score: number | null;
  mock_objective: number | null;
  score_source: string | null;
  error_class: string | null;
  elapsed_seconds: number | null;
  created_at: string;
  updated_at: string;
};

type JobDetailBody = Job & {
  config: Record<string, unknown>;
  metrics: Record<string, unknown>;
  provenance: Record<string, unknown>;
  events: { state: string; note: string; at: string }[];
  lease_expires_at: string | null;
  error_message: string | null;
  dataset: { region: string; manifest_sha256: string; reference_build: string; scoring_version: string } | null;
};

export function JobsPage() {
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const query = useLive<{ items: Job[]; next_cursor: string | null }>(
    ["jobs", state, q],
    `/api/v1/jobs?limit=50${state ? `&state=${encodeURIComponent(state)}` : ""}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
  );
  return (
    <>
      <div className="topbar"><h1>Jobs</h1></div>
      <div className="row">
        <label>Search<input value={q} onChange={(event) => setQ(event.target.value)} /></label>
        <label>
          State
          <select value={state} onChange={(event) => setState(event.target.value)}>
            <option value="">Any</option>
            {["PENDING", "LEASED", "RUNNING", "COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"].map((item) => <option key={item}>{item}</option>)}
          </select>
        </label>
      </div>
      {query.isPending ? <p>Loading jobs…</p> : null}
      {query.isError ? <QueryError error={query.error} /> : null}
      {query.data ? (
        <div className="panel">
          {query.data.items.length === 0 ? <p>No jobs match.</p> : (
            <table>
              <thead><tr><th>Job</th><th>State</th><th>Dataset</th><th>Worker</th><th>Attempt</th><th>Score</th><th>Duration</th></tr></thead>
              <tbody>
                {query.data.items.map((job) => (
                  <tr key={job.job_id}>
                    <td><Link to={`/jobs/${job.job_id}`}>{job.job_id.slice(0, 8)}</Link></td>
                    <td>{job.state}</td>
                    <td>{job.dataset_id}<div className="muted">{job.role}</div></td>
                    <td>{job.worker_id ?? "—"}</td>
                    <td>{job.attempt}</td>
                    <td><Score value={job.score_source === "official" ? job.official_score : job.mock_objective} source={job.score_source} /></td>
                    <td>{formatDuration(job.elapsed_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </>
  );
}

export function JobDetail() {
  const { id = "" } = useParams();
  const query = useLive<JobDetailBody>(["job", id], `/api/v1/jobs/${id}`);
  const logs = useLive<{ excerpt: string; source: string | null; note: string | null }>(["job-log", id], `/api/v1/jobs/${id}/logs?tail=4000`);
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const retry = useMutation({
    mutationFn: () => api(`/api/v1/jobs/${id}/retry`, { method: "POST" }),
    onSuccess: async () => {
      setError(null);
      await client.invalidateQueries();
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Retry failed"),
  });
  if (query.isPending) return <p>Loading job…</p>;
  if (query.isError) return <QueryError error={query.error} />;
  const job = query.data;
  const score = job.score_source === "official" ? job.official_score : job.mock_objective;
  return (
    <>
      <div className="topbar"><h1>Job {job.job_id.slice(0, 8)}</h1><p>{job.state}</p></div>
      <section className="panel">
        <p>Experiment <Link to={`/experiments/${job.experiment_id}`}>{job.experiment_id.slice(0, 8)}</Link> · trial {job.trial_id.slice(0, 8)} · dataset {job.dataset_id}</p>
        <p>Worker {job.worker_id ?? "unassigned"} · attempt {job.attempt} · lease expires <When value={job.lease_expires_at} /></p>
        <p>Score <Score value={score} source={job.score_source} /> · duration {formatDuration(job.elapsed_seconds)}</p>
        <p>Created <When value={job.created_at} /> · updated <When value={job.updated_at} /></p>
        {job.dataset ? <p>Region {job.dataset.region} · {job.dataset.reference_build} · manifest {job.dataset.manifest_sha256.slice(0, 12)} · scoring {job.dataset.scoring_version}</p> : null}
        {job.error_class && (job.state === "FAILED" || job.state === "TIMED_OUT") ? (
          <p className="error">{job.error_class}: {job.error_message}</p>
        ) : null}
        <button type="button" className="mutate" disabled={retry.isPending || !["FAILED", "TIMED_OUT"].includes(job.state)} onClick={() => retry.mutate()}>
          {retry.isPending ? "Retrying…" : "Retry job"}
        </button>
        {error ? <p className="error" role="alert">{error}</p> : null}
      </section>
      <section className="panel">
        <h2>Parameters</h2>
        <pre>{JSON.stringify(job.config, null, 2)}</pre>
      </section>
      <section className="panel">
        <h2>State transitions</h2>
        <table>
          <thead><tr><th>When</th><th>State</th><th>Note</th></tr></thead>
          <tbody>
            {job.events.map((event) => (
              <tr key={event.at + event.state + event.note}><td><When value={event.at} /></td><td>{event.state}</td><td>{event.note}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>Log tail</h2>
        {logs.data?.note ? <p className="muted">{logs.data.note}</p> : null}
        <pre>{logs.data?.excerpt || ""}</pre>
      </section>
      <section className="panel">
        <h2>Metrics</h2>
        <p className="muted">SNP and INDEL fields here are hap.py metrics when the worker sent them. They are not substituted for the official score.</p>
        <pre>{JSON.stringify(job.metrics, null, 2)}</pre>
        <h2>Provenance</h2>
        <pre>{JSON.stringify(job.provenance, null, 2)}</pre>
      </section>
    </>
  );
}
