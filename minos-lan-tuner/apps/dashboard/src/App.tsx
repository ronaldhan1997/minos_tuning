import { useQuery } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { api, ApiError, setCsrf } from "./app/api";
import { QueryError } from "./components";
import { ActivityPage } from "./features/Activity";
import { DatasetsPage } from "./features/Datasets";
import { ExperimentDetail } from "./features/ExperimentDetail";
import { ExperimentsPage } from "./features/Experiments";
import { JobDetail, JobsPage } from "./features/Jobs";
import { OverviewPage } from "./features/Overview";
import { SettingsPage } from "./features/Settings";
import { WorkersPage } from "./features/Workers";

type Session = {
  authenticated: boolean;
  username: string | null;
  csrf: string | null;
  role: string | null;
};

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Shell />}>
          <Route index element={<OverviewPage />} />
          <Route path="experiments" element={<ExperimentsPage />} />
          <Route path="experiments/:id" element={<ExperimentDetail />} />
          <Route path="workers" element={<WorkersPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="jobs/:id" element={<JobDetail />} />
          <Route path="datasets" element={<DatasetsPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="activity" element={<ActivityPage />} />
        </Route>
      </Route>
    </Routes>
  );
}

function RequireAuth() {
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      const body = await api<Session>("/api/v1/auth/session");
      setCsrf(body.csrf);
      return body;
    },
  });
  if (session.isPending) {
    return <p className="main">Checking the session…</p>;
  }
  if (session.isError) {
    return <div className="main"><QueryError error={session.error} /></div>;
  }
  if (!session.data.authenticated) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

function Shell() {
  const navigate = useNavigate();
  return (
    <div className="page">
      <header className="head">
        <div>
          <p className="brand">Minos tuner</p>
          <nav aria-label="Primary">
            <NavLink to="/" end>Home</NavLink>
            <NavLink to="/workers">Computers</NavLink>
            <NavLink to="/experiments">Runs</NavLink>
          </nav>
        </div>
        <button
          type="button"
          className="secondary"
          onClick={async () => {
            await api("/api/v1/auth/logout", { method: "POST" });
            setCsrf(null);
            navigate("/login");
          }}
        >
          Log out
        </button>
      </header>
      <Outlet />
    </div>
  );
}

function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const body = await api<Session>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setCsrf(body.csrf);
      navigate("/");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Login failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="login panel">
      <h1>Sign in</h1>
      <form onSubmit={onSubmit} className="grid" style={{ gridTemplateColumns: "1fr" }}>
        <label>
          Username
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error ? <p className="error" role="alert">{error}</p> : null}
        <button type="submit" disabled={pending}>{pending ? "Checking…" : "Sign in"}</button>
      </form>
    </main>
  );
}
