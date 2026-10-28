import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, ApiError } from "../app/api";
import { useLive } from "../app/live";
import { QueryError } from "../components";

type Dataset = {
  dataset_id: string;
  name: string;
  region: string;
  chromosome: string | null;
  reference_build: string;
  manifest_sha256: string;
  reference_sha256: string;
  scoring_version: string;
  source: string;
  license_note: string;
  total_bytes: number;
  roles: string[];
  workers_reporting_cache: string[];
};

export function DatasetsPage() {
  const query = useLive<{ items: Dataset[] }>(["datasets"], "/api/v1/datasets/registered");
  return (
    <>
      <div className="topbar"><h1>Datasets</h1></div>
      <p className="muted">Manifest hashes only. BAM and truth files are not offered for download in this view.</p>
      {query.isPending ? <p>Loading datasets…</p> : null}
      {query.isError ? <QueryError error={query.error} /> : null}
      {query.data ? (
        <div className="panel">
          {query.data.items.length === 0 ? <p>No datasets imported.</p> : (
            <table>
              <thead><tr><th>Name</th><th>Region</th><th>Build</th><th>Scoring</th><th>Manifest</th><th>Cache reports</th><th>License</th></tr></thead>
              <tbody>
                {query.data.items.map((row) => (
                  <tr key={row.dataset_id}>
                    <td>{row.name}</td>
                    <td>{row.chromosome} {row.region}</td>
                    <td>{row.reference_build}</td>
                    <td>{row.scoring_version}</td>
                    <td title={row.manifest_sha256}>{row.manifest_sha256.slice(0, 12)}</td>
                    <td>{row.workers_reporting_cache.length ? row.workers_reporting_cache.join(", ") : "none reported"}</td>
                    <td>{row.license_note || row.source || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
      <ImportPanel />
    </>
  );
}

function ImportPanel() {
  const client = useQueryClient();
  const [manifest, setManifest] = useState("");
  const [samplePath, setSamplePath] = useState("");
  const [version, setVersion] = useState("v1");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: async (kind: "json" | "practice") => {
      if (kind === "practice") {
        return api("/api/v1/datasets/import-practice", {
          method: "POST",
          body: JSON.stringify({ sample_path: samplePath, scoring_version: version }),
        });
      }
      return api("/api/v1/datasets/import", {
        method: "POST",
        body: JSON.stringify({ manifest: JSON.parse(manifest) as object }),
      });
    },
    onSuccess: async () => {
      setError(null);
      setMessage("Import accepted.");
      await client.invalidateQueries({ queryKey: ["datasets"] });
    },
    onError: (caught) => setError(caught instanceof ApiError ? caught.message : "Import failed"),
  });
  function onJson(event: FormEvent) {
    event.preventDefault();
    try {
      JSON.parse(manifest);
    } catch {
      setError("Manifest text is not JSON.");
      return;
    }
    mutation.mutate("json");
  }
  return (
    <div className="panel mutate">
      <h2>Import an authorized local dataset</h2>
      <p className="muted">Practice paths must sit under the coordinator's dataset roots. This does not publish the files.</p>
      <form onSubmit={(event) => { event.preventDefault(); mutation.mutate("practice"); }} className="grid">
        <label>sample.json path<input value={samplePath} onChange={(event) => setSamplePath(event.target.value)} /></label>
        <label>Scoring version
          <select value={version} onChange={(event) => setVersion(event.target.value)}>
            <option>v1</option>
            <option>v2</option>
          </select>
        </label>
        <button type="submit" disabled={mutation.isPending || !samplePath}>Import practice sample</button>
      </form>
      <form onSubmit={onJson}>
        <label>Manifest JSON<textarea value={manifest} onChange={(event) => setManifest(event.target.value)} /></label>
        <button type="submit" disabled={mutation.isPending || !manifest}>Import manifest</button>
      </form>
      {message ? <p>{message}</p> : null}
      {error ? <p className="error" role="alert">{error}</p> : null}
    </div>
  );
}
