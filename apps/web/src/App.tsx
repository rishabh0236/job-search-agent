/**
 * App shell and routing.
 *
 * Hash routing keeps deep links working with no router dependency and no server-side
 * rewrite rules — the whole UI is a static bundle FastAPI can serve from one path.
 */

import { useState } from "react";

import { api } from "./lib/api";
import { useAction, useAsync, useCandidateId, useRoute } from "./lib/hooks";
import { ErrorBox } from "./components/ui";
import { Dashboard } from "./screens/Dashboard";
import { JobDetail, JobFeed } from "./screens/Jobs";
import { Profile } from "./screens/Profile";
import { Tailor } from "./screens/Tailor";
import { ApplicationReview, ApplicationTracker, StartApplication } from "./screens/Applications";

const NAV: { screen: string; label: string; group: string }[] = [
  { screen: "dashboard", label: "Dashboard", group: "Overview" },
  { screen: "profile", label: "Candidate profile", group: "Candidate" },
  { screen: "jobs", label: "Job discovery", group: "Jobs" },
  { screen: "applications", label: "Applications", group: "Apply" },
];

export function App() {
  const [route, navigate] = useRoute();
  const [candidateId, setCandidateId] = useCandidateId();
  const { run, pending, error } = useAction();
  const [name, setName] = useState("");
  const [dark, setDark] = useState(() => document.documentElement.getAttribute("data-theme") === "dark");

  const toggleTheme = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.setAttribute("data-theme", next ? "dark" : "light");
    localStorage.setItem("theme", next ? "dark" : "light");
  };

  const profile = useAsync(
    () => (candidateId ? api.profile(candidateId).catch(() => undefined) : Promise.resolve(undefined)),
    [candidateId],
  );
  const applications = useAsync(
    () => (candidateId ? api.applications(candidateId).catch(() => []) : Promise.resolve([])),
    [candidateId, route.screen],
  );

  const needsReview = (applications.data ?? []).filter(
    (item) => item.status === "ready_for_review",
  ).length;
  const unverified = (profile.data?.facts ?? []).filter((fact) => !fact.verified).length;

  const createCandidate = () =>
    void run(async () => {
      const created = await api.createCandidate(name.trim() || "Candidate");
      setCandidateId(created.id);
      navigate("profile");
    });

  const counts: Record<string, number> = { profile: unverified, applications: needsReview };
  const groups = [...new Set(NAV.map((item) => item.group))];

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="between brand">
          <div className="row">
            <div className="brand-mark" aria-hidden="true">CA</div>
            <div>
              <div className="brand-name">Career Agent</div>
              <div className="brand-sub">evidence-grounded</div>
            </div>
          </div>
          <button
            className="btn-sm btn-ghost"
            aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
            title={dark ? "Switch to light theme" : "Switch to dark theme"}
            onClick={toggleTheme}
          >
            {dark ? "☀" : "☾"}
          </button>
        </div>

        <nav className="nav" aria-label="Main">
          {groups.map((group) => (
            <div key={group} style={{ marginBottom: "0.5rem" }}>
              <div className="nav-group-label">{group}</div>
              {NAV.filter((item) => item.group === group).map((item) => (
                <button
                  key={item.screen}
                  className="nav-item"
                  aria-current={route.screen === item.screen ? "page" : undefined}
                  onClick={() => navigate(item.screen)}
                >
                  <span>{item.label}</span>
                  {counts[item.screen] ? (
                    <span className="nav-count">{counts[item.screen]}</span>
                  ) : null}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div style={{ marginTop: "auto" }} className="stack">
          {candidateId ? (
            <>
              <div className="subtle">Active candidate</div>
              <div className="mono">{candidateId.slice(0, 18)}…</div>
              <button className="btn-sm btn-ghost" onClick={() => setCandidateId(null)}>
                Switch candidate
              </button>
            </>
          ) : (
            <div className="stack">
              <label htmlFor="candidate-name">New candidate</label>
              <input
                id="candidate-name"
                value={name}
                placeholder="Your name"
                onChange={(event) => setName(event.target.value)}
              />
              <button className="btn-sm btn-primary" disabled={pending} onClick={createCandidate}>
                Create
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="content">
        {error ? <ErrorBox error={error} /> : null}
        <Screen route={route} candidateId={candidateId} navigate={navigate} onCreate={createCandidate} />
      </main>
    </div>
  );
}

function Screen({
  route,
  candidateId,
  navigate,
  onCreate,
}: {
  route: { screen: string; params: Record<string, string> };
  candidateId: string | null;
  navigate: (screen: string, params?: Record<string, string>) => void;
  onCreate: () => void;
}) {
  if (route.screen === "dashboard") {
    return <Dashboard candidateId={candidateId} onNavigate={navigate} onCreateCandidate={onCreate} />;
  }

  if (!candidateId) {
    return (
      <div className="empty">
        <h3>No candidate selected</h3>
        <p>Create a candidate in the sidebar to continue.</p>
      </div>
    );
  }

  switch (route.screen) {
    case "profile":
      return <Profile candidateId={candidateId} />;
    case "jobs":
      return <JobFeed candidateId={candidateId} onNavigate={navigate} />;
    case "job":
      return route.params.id ? (
        <>
          <JobDetail candidateId={candidateId} jobId={route.params.id} onNavigate={navigate} />
          <StartApplication candidateId={candidateId} jobId={route.params.id} onNavigate={navigate} />
        </>
      ) : (
        <JobFeed candidateId={candidateId} onNavigate={navigate} />
      );
    case "tailor":
      return route.params.id ? (
        <Tailor candidateId={candidateId} jobId={route.params.id} onNavigate={navigate} />
      ) : null;
    case "applications":
      return <ApplicationTracker candidateId={candidateId} onNavigate={navigate} />;
    case "review":
      return route.params.id ? (
        <ApplicationReview applicationId={route.params.id} onNavigate={navigate} />
      ) : (
        <ApplicationTracker candidateId={candidateId} onNavigate={navigate} />
      );
    default:
      return (
        <div className="empty">
          <h3>Screen not found</h3>
          <p>
            <a href="#/dashboard">Back to the dashboard</a>
          </p>
        </div>
      );
  }
}
