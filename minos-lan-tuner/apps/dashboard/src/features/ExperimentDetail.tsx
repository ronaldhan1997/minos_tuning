import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, ApiError } from "../app/api";
import { formatScore } from "../app/format";
import { useLive } from "../app/live";
import { QueryError } from "../components";

type Experiment = {
  id: string;
  name: string;
  status: string;
  scoring_version: string;
  budget: number;
  trials_told: number;
  training_dataset_ids: string[];
  held_out_dataset_ids: string[];
  selection: Record<string, unknown> | null;
};

type TrialRow = {
  state: string;
  objective: number | null;
  score_source: string | null;
  jobs: { state: string }[];
};

type ExportResult = {
  downloads: { conf: string };
};

const STATUS: Record<string, string> = {
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

export function ExperimentDetail() {
  const { id = "" } = useParams();
  const experiment = useLive<Experiment>(["experiment", id], `/api/v1/experiments/${id}`);
  const trials = useLive<{ trials: TrialRow[] }>(["trials", id], `/api/v1/experiments/${id}/trials`);
  if (experiment.isPending) return <p>Loading…</p>;
  if (experiment.isError) return <QueryError error={experiment.error} />;
  const row = experiment.data;
  const jobs = (trials.data?.trials ?? []).flatMap((trial) => trial.jobs);
  const waiting = jobs.filter((job) => job.state === "PENDING").length;
  const running = jobs.filter((job) => job.state === "RUNNING" || job.state === "LEASED").length;
  const best = (trials.data?.trials ?? []).reduce<{ value: number; source: string | null } | null>((current, trial) => {
    if (trial.objective === null) return current;
    if (current === null || trial.objective > current.value) {
      return { value: trial.objective, source: trial.score_source };
    }
    return current;
  }, null);
  return (
    <>
      <h1>{row.name}</h1>
      <section className="panel">
        <p className="run-title">{STATUS[row.status] ?? row.status}</p>
        <p>{row.trials_told} of {row.budget} finished</p>
        <progress value={row.budget ? Math.min(row.trials_told, row.budget) : 0} max={row.budget || 1} aria-label="Finished trials" />
        <p>Sample {row.training_dataset_ids[0] ?? "—"}</p>
        <p>Score {row.scoring_version}</p>
        <p>Best score {best ? formatScore(best.value, best.source) : "—"}</p>
        {waiting || running ? <p>{running} running · {waiting} waiting</p> : null}
        <Actions experiment={row} />
      </section>
    </>
  );
}

function Actions({ experiment }: { experiment: Experiment }) {
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [download, setDownload] = useState<string | null>(null);
  const command = useMutation({
    mutationFn: (path: string) => api(`/api/v1/experiments/${experiment.id}/${path}`, { method: "POST" }),
    onSuccess: async () => {
      setError(null);
      await client.invalidateQueries();
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "The command failed."),
  });
  const doExport = useMutation({
    mutationFn: () => api<ExportResult>(`/api/v1/experiments/${experiment.id}/export`, { method: "POST" }),
    onSuccess: (body) => setDownload(body.downloads.conf),
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Export failed."),
  });
  const status = experiment.status;
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
      {status === "COMPLETED" ? (
        <button type="button" disabled={doExport.isPending} onClick={() => doExport.mutate()}>Download config</button>
      ) : null}
      {download ? <a href={download}>gatk.conf</a> : null}
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}
