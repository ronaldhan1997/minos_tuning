import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, ApiError, liveInterval } from "../app/api";
import { formatCpu, formatDuration, formatScore, formatShare, sharePercent } from "../app/format";
import { useLive } from "../app/live";
import { LineChart, QueryError, Usage } from "../components";

type Overview = {
  active_experiment: {
    id: string;
    name: string;
    status: string;
    budget: number;
    trials_told: number;
    best_objective: number | null;
    best_score_source: string | null;
  } | null;
  workers: { online: number; total: number };
  jobs: { active: number; queued: number; failed: number };
  best_validated: { held_out_mean: number; score_source: string } | null;
  best_training: { training_objective: number; score_source: string; validated: boolean } | null;
};

type Worker = {
  worker_id: string;
  hostname: string;
  presence: string;
  cpu_count: number;
  cpu_percent: number | null;
  ram_mb: number;
  ram_used_mb: number | null;
  free_disk_mb: number;
  disk_total_mb: number | null;
};

type Job = {
  job_id: string;
  state: string;
  worker_id: string | null;
  official_score: number | null;
  mock_objective: number | null;
  score_source: string | null;
};

const RUN_STATUS: Record<string, string> = {
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

const JOB_STATUS: Record<string, string> = {
  PENDING: "Waiting",
  LEASED: "Assigned",
  RUNNING: "Running",
  COMPLETED: "Done",
  FAILED: "Failed",
  TIMED_OUT: "Timed out",
  CANCELLED: "Cancelled",
};

export function computerStatus(presence: string): string {
  if (presence === "online-idle") return "Online";
  if (presence === "online-busy") return "Working";
  if (presence === "offline") return "Offline";
  if (presence === "paused") return "Paused";
  if (presence === "draining") return "Finishing";
  if (presence === "incompatible") return "Cannot take jobs";
  return "Unknown";
}

export function OverviewPage() {
  const overview = useLive<Overview>(["overview"], "/api/v1/overview");
  const workers = useLive<{ items: Worker[] }>(["workers"], "/api/v1/workers?limit=100");
  const jobs = useLive<{ items: Job[] }>(["jobs", "home"], "/api/v1/jobs?limit=8");
  if (overview.isPending) return <p>Loading…</p>;
  if (overview.isError) return <QueryError error={overview.error} />;
  const data = overview.data;
  const active = data.active_experiment;
  const best = data.best_validated
    ? formatScore(data.best_validated.held_out_mean, data.best_validated.score_source)
    : data.best_training
      ? formatScore(data.best_training.training_objective, data.best_training.score_source)
      : "—";
  const done = active ? active.trials_told : 0;
  const budget = active ? active.budget : 0;
  return (
    <>
      <h1>Tuner</h1>
      <div className="cards">
        <article className="card">
          <div className="k">Computers online</div>
          <div className="v">{data.workers.online} / {data.workers.total}</div>
        </article>
        <article className="card">
          <div className="k">Jobs running</div>
          <div className="v">{data.jobs.active}</div>
        </article>
        <article className="card">
          <div className="k">Best score</div>
          <div className="v">{best}</div>
        </article>
      </div>

      <section className="panel">
        <h2>Computers</h2>
        {workers.isError ? <QueryError error={workers.error} /> : null}
        {workers.data && workers.data.items.length === 0 ? (
          <p>No computer is connected. On the Windows PC, double-click MinosClient.exe and enter this server’s address.</p>
        ) : null}
        {workers.data && workers.data.items.length > 0 ? (
          <table>
            <thead>
              <tr><th>Computer</th><th>Status</th><th>CPU</th><th>Memory</th><th>Disk</th></tr>
            </thead>
            <tbody>
              {workers.data.items.map((worker) => (
                <tr key={worker.worker_id}>
                  <td>{worker.hostname || worker.worker_id}</td>
                  <td>{computerStatus(worker.presence)}</td>
                  <td><Usage label="CPU" percent={worker.cpu_percent} detail={formatCpu(worker.cpu_percent)} /></td>
                  <td><Usage label="Memory" percent={sharePercent(worker.ram_used_mb, worker.ram_mb)} detail={formatShare(worker.ram_used_mb, worker.ram_mb)} /></td>
                  <td><Usage label="Disk" percent={sharePercent(worker.disk_total_mb == null ? null : worker.disk_total_mb - worker.free_disk_mb, worker.disk_total_mb)} detail={formatShare(worker.disk_total_mb == null ? null : worker.disk_total_mb - worker.free_disk_mb, worker.disk_total_mb)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>

      <section className="panel">
        <h2>Current run</h2>
        {active ? (
          <>
            <p className="run-title">{active.name}</p>
            <p>{RUN_STATUS[active.status] ?? active.status}</p>
            <p>{done} of {budget} finished{data.jobs.queued ? ` · ${data.jobs.queued} waiting` : ""}{data.jobs.failed ? ` · ${data.jobs.failed} failed` : ""}</p>
            <progress value={budget ? Math.min(done, budget) : 0} max={budget || 1} aria-label="Finished trials" />
            <p>Best score {formatScore(active.best_objective, active.best_score_source)}</p>
            <RunLog experimentId={active.id} />
            <RunButtons id={active.id} status={active.status} />
          </>
        ) : (
          <p>No run is active. <Link to="/experiments">Start a run</Link></p>
        )}
      </section>

      <Performance experimentId={active?.id ?? null} />

      <section className="panel">
        <h2>Latest jobs</h2>
        {jobs.isError ? <QueryError error={jobs.error} /> : null}
        {jobs.data && jobs.data.items.length === 0 ? <p>No jobs yet.</p> : null}
        {jobs.data && jobs.data.items.length > 0 ? (
          <table>
            <thead>
              <tr><th>Status</th><th>Computer</th><th>Score</th></tr>
            </thead>
            <tbody>
              {jobs.data.items.map((job) => (
                <tr key={job.job_id}>
                  <td><Link to={`/jobs/${job.job_id}`}>{JOB_STATUS[job.state] ?? job.state}</Link></td>
                  <td>{job.worker_id ?? "—"}</td>
                  <td>{formatScore(job.official_score ?? job.mock_objective, job.score_source)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </section>
    </>
  );
}

type Metrics = {
  status: string;
  throughput_completed_jobs_last_hour: number;
  mean_elapsed_seconds: number | null;
  eta: { seconds: number } | null;
  best_so_far: { optuna_number: number; best_so_far: number }[];
};

function Performance({ experimentId }: { experimentId: string | null }) {
  const metrics = useQuery({
    queryKey: ["metrics", experimentId],
    queryFn: () => api<Metrics>(`/api/v1/experiments/${experimentId}/metrics`),
    enabled: Boolean(experimentId),
    refetchInterval: liveInterval,
  });
  return (
    <section className="panel">
      <h2>Performance</h2>
      {experimentId === null ? <p>No run is active.</p> : null}
      {metrics.isLoading ? <p>Loading performance…</p> : null}
      {metrics.isError ? <QueryError error={metrics.error} /> : null}
      {metrics.data ? (
        <>
          <div className="cards">
            <article className="card">
              <div className="k">Finished last hour</div>
              <div className="v">{metrics.data.throughput_completed_jobs_last_hour}</div>
            </article>
            <article className="card">
              <div className="k">Average job</div>
              <div className="v">{formatDuration(metrics.data.mean_elapsed_seconds)}</div>
            </article>
            <article className="card">
              <div className="k">Time left</div>
              <div className="v">{formatDuration(metrics.data.eta?.seconds)}</div>
            </article>
          </div>
          {metrics.data.eta === null && (metrics.data.status === "RUNNING" || metrics.data.status === "VALIDATING") ? (
            <p className="muted">Time left appears after three finished jobs.</p>
          ) : null}
          <LineChart
            points={metrics.data.best_so_far.map((point) => ({ x: String(point.optuna_number), y: point.best_so_far }))}
            label="Best score"
          />
        </>
      ) : null}
    </section>
  );
}

type RunLogBody = {
  items: { job_id: string; worker_id: string | null; state: string; excerpt: string }[];
};

function RunLog({ experimentId }: { experimentId: string }) {
  const box = useRef<HTMLPreElement>(null);
  const log = useQuery({
    queryKey: ["run-log", experimentId],
    queryFn: () => api<RunLogBody>(`/api/v1/experiments/${experimentId}/log`),
    refetchInterval: () => (document.hidden ? false : 2000),
  });
  const lines = (log.data?.items ?? []).map((item) => {
    const who = item.worker_id ?? "a computer";
    const body = item.excerpt.trim();
    return body ? `${who}\n${body}` : `${who}\nWaiting for output from this job.`;
  });
  const text = log.isError
    ? "The log could not be loaded."
    : lines.length === 0
      ? "No job is running."
      : lines.join("\n\n");
  useEffect(() => {
    const node = box.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [text]);
  return (
    <pre className="run-log" ref={box} aria-live="polite" aria-label="Running log">
      {log.isPending ? "Loading log…" : text}
    </pre>
  );
}

function RunButtons({ id, status }: { id: string; status: string }) {
  const client = useQueryClient();
  const command = useMutation({
    mutationFn: (path: string) => api(`/api/v1/experiments/${id}/${path}`, { method: "POST" }),
    onSuccess: async () => {
      await client.invalidateQueries();
    },
  });
  const error = command.error instanceof ApiError ? command.error.message : command.isError ? "The command failed." : null;
  return (
    <div className="row">
      {status === "DRAFT" ? <button type="button" disabled={command.isPending} onClick={() => command.mutate("start")}>Start</button> : null}
      {status === "RUNNING" || status === "VALIDATING" ? (
        <button type="button" className="secondary" disabled={command.isPending} onClick={() => command.mutate("pause")}>Pause</button>
      ) : null}
      {status === "PAUSED" ? <button type="button" disabled={command.isPending} onClick={() => command.mutate("resume")}>Resume</button> : null}
      {status === "RUNNING" || status === "VALIDATING" || status === "PAUSED" || status === "DRAFT" ? (
        <button type="button" className="secondary" disabled={command.isPending} onClick={() => command.mutate("stop")}>Stop</button>
      ) : null}
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}
