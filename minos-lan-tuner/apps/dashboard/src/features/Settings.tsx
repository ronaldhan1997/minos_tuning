import { useLive } from "../app/live";
import { QueryError, When } from "../components";

type Status = {
  api_version: string;
  minos_commit: string;
  pinned_commit: string;
  commit_matches_pin: boolean;
  gatk_image: string;
  gatk_image_id: string | null;
  happy_image: string;
  bcftools_image: string;
  scorer_versions: string[];
  scorer_names: Record<string, string>;
  database: string;
  heartbeat_stale_seconds: number;
  server_time: string;
  live_submit: boolean;
  http_note: string;
  dashboard_login_configured: boolean;
};

type Overview = { warnings: { code: string; message: string }[] };

export function SettingsPage() {
  const status = useLive<Status>(["system"], "/api/v1/system/status");
  const overview = useLive<Overview>(["overview"], "/api/v1/overview");
  if (status.isPending) return <p>Loading settings…</p>;
  if (status.isError) return <QueryError error={status.error} />;
  const row = status.data;
  return (
    <>
      <div className="topbar"><h1>Settings</h1><p className="muted">Server time <When value={row.server_time} /></p></div>
      <section className="panel">
        <p>API {row.api_version} · database {row.database}</p>
        <p>Minos commit {row.minos_commit}</p>
        <p>Pinned commit {row.pinned_commit} · {row.commit_matches_pin ? "matches" : "does not match"}</p>
        <p>GATK image {row.gatk_image}</p>
        <p>GATK digest {row.gatk_image_id ?? "not present on this host"}</p>
        <p>hap.py {row.happy_image}</p>
        <p>bcftools {row.bcftools_image}</p>
        <p>Scorers {row.scorer_versions.map((version) => `${version} ${row.scorer_names[version]}`).join(", ")}</p>
        <p>Heartbeat is stale after {row.heartbeat_stale_seconds}s.</p>
        <p>Live Minos submission from this dashboard: {row.live_submit ? "enabled" : "disabled"}.</p>
        <p className="warnbox">{row.http_note}</p>
        <p>Browser login configured: {row.dashboard_login_configured ? "yes" : "no"}.</p>
      </section>
      <section className="panel">
        <h2>Warnings</h2>
        {overview.data?.warnings.length ? overview.data.warnings.map((item) => <p key={item.message}>{item.message}</p>) : <p>No active warnings.</p>}
      </section>
    </>
  );
}
