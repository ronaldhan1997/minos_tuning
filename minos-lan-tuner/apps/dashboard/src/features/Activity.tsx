import { useLive } from "../app/live";
import { QueryError, When } from "../components";

type Audit = {
  items: { id: string; at: string; actor: string; action: string; target_type: string; target_id: string; result: string }[];
};

export function ActivityPage() {
  const query = useLive<Audit>(["audit"], "/api/v1/audit?limit=100");
  if (query.isPending) return <p>Loading activity…</p>;
  if (query.isError) return <QueryError error={query.error} />;
  return (
    <>
      <div className="topbar"><h1>Activity</h1></div>
      <div className="panel">
        {query.data.items.length === 0 ? <p>No audit events yet.</p> : (
          <table>
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Result</th></tr></thead>
            <tbody>
              {query.data.items.map((row) => (
                <tr key={row.id}>
                  <td><When value={row.at} /></td>
                  <td>{row.actor}</td>
                  <td>{row.action}</td>
                  <td>{row.target_type} {row.target_id.slice(0, 12)}</td>
                  <td>{row.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
