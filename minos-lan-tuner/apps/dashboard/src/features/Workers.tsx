import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, ApiError } from "../app/api";
import { formatCpu, formatShare, formatWhen, sharePercent } from "../app/format";
import { useLive } from "../app/live";
import { QueryError, Usage } from "../components";
import { computerStatus } from "./Overview";

type Worker = {
  worker_id: string;
  hostname: string;
  host_os: string;
  platform: string;
  presence: string;
  runtime_note: string;
  heartbeat_age_seconds: number | null;
  last_heartbeat: string | null;
  cpu_count: number;
  cpu_percent: number | null;
  ram_mb: number;
  ram_used_mb: number | null;
  free_disk_mb: number;
  disk_total_mb: number | null;
  docker_ready: boolean;
  max_parallel_jobs: number;
  effective_max_parallel: number;
  scorer_version: string;
  scorer_name: string;
  minos_commit: string;
  execution_mode: string;
  scheduling_mode: string;
  active_job_ids: string[];
  stale: boolean;
};

export function WorkersPage() {
  const query = useLive<{ items: Worker[] }>(["workers"], "/api/v1/workers?limit=100");
  const [error, setError] = useState<string | null>(null);
  if (query.isPending) return <p>Loading workers…</p>;
  if (query.isError) return <QueryError error={query.error} />;
  return (
    <>
      <h1>Computers</h1>
      {error ? <p className="error" role="alert">{error}</p> : null}
      <TokenPanel />
      <div className="panel">
        {query.data.items.length === 0 ? (
          <p>No computer is connected. On the Windows PC, double-click MinosClient.exe and enter this server’s address.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Computer</th><th>Status</th><th>CPU</th><th>Memory</th><th>Disk</th><th></th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((worker) => (
                <tr key={worker.worker_id}>
                  <td>{worker.hostname || worker.worker_id}</td>
                  <td>{computerStatus(worker.presence)}</td>
                  <td><Usage label="CPU" percent={worker.cpu_percent} detail={formatCpu(worker.cpu_percent)} /></td>
                  <td><Usage label="Memory" percent={sharePercent(worker.ram_used_mb, worker.ram_mb)} detail={formatShare(worker.ram_used_mb, worker.ram_mb)} /></td>
                  <td><Usage label="Disk" percent={sharePercent(worker.disk_total_mb == null ? null : worker.disk_total_mb - worker.free_disk_mb, worker.disk_total_mb)} detail={formatShare(worker.disk_total_mb == null ? null : worker.disk_total_mb - worker.free_disk_mb, worker.disk_total_mb)} /></td>
                  <td><WorkerActions worker={worker} onError={setError} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

type IssuedToken = { worker_id: string; token: string };
type TokenRow = { worker_id: string; revoked: boolean; created_at: string; registered: boolean };

const COMPUTER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

function serverAddress(): string {
  const origin = window.location.origin;
  if (/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(origin)) {
    return "Open this page from the laptop. The address in that browser is the server address.";
  }
  return `Server address ${origin}`;
}

function TokenPanel() {
  const client = useQueryClient();
  const tokens = useLive<{ items: TokenRow[] }>(["worker-tokens"], "/api/v1/workers/tokens");
  const [name, setName] = useState("");
  const [issued, setIssued] = useState<IssuedToken | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const existing = tokens.data?.items.some((row) => row.worker_id === name.trim() && !row.revoked) ?? false;
  const generate = useMutation({
    mutationFn: async (workerId: string) =>
      api<IssuedToken>("/api/v1/workers/tokens", { method: "POST", body: JSON.stringify({ worker_id: workerId }) }),
    onSuccess: async (row) => {
      setError(null);
      setCopied(false);
      setIssued(row);
      setName("");
      await client.invalidateQueries({ queryKey: ["worker-tokens"] });
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Could not generate a token"),
  });
  const revoke = useMutation({
    mutationFn: async (workerId: string) => api(`/api/v1/workers/${encodeURIComponent(workerId)}/revoke-token`, { method: "POST" }),
    onSuccess: async () => {
      setError(null);
      await client.invalidateQueries({ queryKey: ["worker-tokens"] });
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Could not revoke the token"),
  });

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const workerId = name.trim();
    if (!COMPUTER_NAME.test(workerId)) {
      setError("Use letters, numbers, dots, underscores, or hyphens.");
      return;
    }
    generate.mutate(workerId);
  }

  async function copyToken() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.token);
      setCopied(true);
    } catch {
      setError("Select the token and copy it.");
    }
  }

  return (
    <form className="panel" onSubmit={onSubmit}>
      <h2>Tokens</h2>
      <p className="muted">Each token works for one computer name. Paste it into Minos tuner on that computer.</p>
      {error ? <p className="error" role="alert">{error}</p> : null}
      <div className="token-form">
        <label>Computer name
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="DESKTOP-ATBBR1B" />
        </label>
        <button type="submit" disabled={generate.isPending}>{existing ? "Replace token" : "Generate token"}</button>
      </div>
      {issued ? (
        <div className="warnbox">
          <p>Token for {issued.worker_id}. Copy it now. Generating again for this name replaces it.</p>
          <p>{serverAddress()}</p>
          <div className="row">
            <input className="token-value" readOnly value={issued.token} onFocus={(event) => event.target.select()} />
            <button type="button" className="secondary" onClick={() => void copyToken()}>{copied ? "Copied" : "Copy"}</button>
          </div>
        </div>
      ) : null}
      {tokens.isError ? <QueryError error={tokens.error} /> : null}
      {tokens.data && tokens.data.items.length === 0 ? <p>No tokens yet.</p> : null}
      {tokens.data && tokens.data.items.length > 0 ? (
        <table>
          <thead>
            <tr><th>Computer</th><th>Status</th><th>Created</th><th></th></tr>
          </thead>
          <tbody>
            {tokens.data.items.map((row) => (
              <tr key={row.worker_id}>
                <td>{row.worker_id}</td>
                <td>{row.revoked ? "Revoked" : row.registered ? "In use" : "Ready"}</td>
                <td>{formatWhen(row.created_at).local}</td>
                <td>
                  {row.revoked ? null : (
                    <button type="button" className="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(row.worker_id)}>Revoke</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </form>
  );
}

function WorkerActions({ worker, onError }: { worker: Worker; onError: (message: string | null) => void }) {
  const client = useQueryClient();
  const command = useMutation({
    mutationFn: async (action: string) => api(`/api/v1/workers/${worker.worker_id}/${action}`, { method: "POST" }),
    onSuccess: async () => {
      onError(null);
      await client.invalidateQueries({ queryKey: ["workers"] });
    },
    onError: (caught) => onError(caught instanceof ApiError ? caught.message : "Worker command failed"),
  });
  return (
    <div className="row">
      {worker.scheduling_mode === "paused" ? (
        <button type="button" disabled={command.isPending} onClick={() => command.mutate("enable")}>Allow jobs</button>
      ) : (
        <button type="button" className="secondary" disabled={command.isPending} onClick={() => command.mutate("pause")}>Pause</button>
      )}
    </div>
  );
}
