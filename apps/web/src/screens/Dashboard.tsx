/**
 * Dashboard — what needs attention, and whether the system can actually do its job.
 *
 * Capability status is shown prominently rather than hidden in a settings page: if
 * LaTeX is missing or no model is configured, the user should learn that here, not
 * when a tailoring run fails.
 */

import { api } from "../lib/api";
import { useAsync } from "../lib/hooks";
import {
  CapabilityDot,
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  ScoreMeter,
  Stat,
  StatusPill,
  TrustLegend,
} from "../components/ui";

interface Props {
  candidateId: string | null;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
  onCreateCandidate: () => void;
}

export function Dashboard({ candidateId, onNavigate, onCreateCandidate }: Props) {
  const health = useAsync(() => api.health(), []);
  const profile = useAsync(
    () => (candidateId ? api.profile(candidateId) : Promise.resolve(undefined)),
    [candidateId],
  );
  const matches = useAsync(
    () => (candidateId ? api.matches(candidateId) : Promise.resolve(undefined)),
    [candidateId],
  );
  const applications = useAsync(
    () => (candidateId ? api.applications(candidateId) : Promise.resolve(undefined)),
    [candidateId],
  );

  const facts = profile.data?.facts ?? [];
  const unverified = facts.filter((fact) => !fact.verified).length;
  const unknown = facts.filter((fact) => fact.provenance === "unknown").length;
  const needsReview = (applications.data ?? []).filter(
    (item) => item.status === "ready_for_review",
  ).length;
  const stopped = (applications.data ?? []).filter((item) => item.status === "stopped").length;
  const topMatches = (matches.data?.matches ?? []).slice(0, 5);

  const name = profile.data?.display_name;

  return (
    <>
      <header className="page-head">
        <h1>{name ? `Welcome back, ${name}` : "Dashboard"}</h1>
        <p>
          Your candidate profile, the jobs it matches, and every application waiting on you.
          Nothing here is sent anywhere without your explicit approval.
        </p>
      </header>

      {!candidateId ? (
        <EmptyState
          title="Start with a candidate profile"
          body="Create a profile, then import your resume. Everything the agent later says about you traces back to that document."
          action={
            <button className="btn-primary" onClick={onCreateCandidate}>
              Create profile
            </button>
          }
        />
      ) : (
        <>
          <div className="section-label">Profile signal</div>
          <div className="grid grid-3">
            <Stat
              label="Canonical facts"
              value={facts.length}
              note={`${unverified} awaiting your confirmation`}
              tone={unverified > 0 ? "warn" : "ok"}
            />
            <Stat
              label="Unknown items"
              value={unknown}
              note="never used until confirmed"
              tone={unknown > 0 ? "warn" : undefined}
            />
            <Stat
              label="Ranked jobs"
              value={matches.data?.matches.length ?? 0}
              note="scored against your profile"
            />
          </div>

          <div className="section-label">Application pipeline</div>
          <div className="grid grid-3">
            <Stat
              label="Awaiting review"
              value={needsReview}
              note="applications ready for you"
              tone={needsReview > 0 ? "warn" : undefined}
            />
            <Stat
              label="Stopped"
              value={stopped}
              note="needed a human decision"
              tone={stopped > 0 ? "danger" : undefined}
            />
            <Stat
              label="Submitted"
              value={(applications.data ?? []).filter((item) => item.status === "submitted").length}
            />
          </div>

          <div style={{ marginTop: "1.6rem" }}>
            <Card
              title="Top matches"
              actions={
                <button className="btn-sm" onClick={() => onNavigate("jobs")}>
                  Open job feed
                </button>
              }
            >
              {matches.loading ? (
                <Loading label="Scoring" />
              ) : topMatches.length === 0 ? (
                <EmptyState
                  title="No scored jobs yet"
                  body="Discover jobs from your configured sources, then score them against your profile."
                  action={
                    <button className="btn-primary" onClick={() => onNavigate("jobs")}>
                      Discover jobs
                    </button>
                  }
                />
              ) : (
                <div className="list">
                  {topMatches.map((match) => {
                    const job = matches.data?.jobs[match.job_id];
                    return (
                      <button
                        key={match.id}
                        className="item"
                        onClick={() => onNavigate("job", { id: match.job_id })}
                      >
                        <div className="between">
                          <div>
                            <div className="item-title">{job?.title ?? match.job_id}</div>
                            <div className="subtle">
                              {job?.company} · {job?.location ?? "location not stated"}
                            </div>
                            <div className="row" style={{ marginTop: "0.35rem" }}>
                              <StatusPill status={match.eligibility} />
                            </div>
                          </div>
                          <ScoreMeter score={match.score} />
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </Card>
          </div>
        </>
      )}

      <div style={{ marginTop: "1.6rem" }}>
        {health.data ? (
          <Card
            title="System"
            badge={
              <span className={`badge ${health.data.status === "ok" ? "badge-ok" : "badge-warn"}`}>
                {health.data.status}
              </span>
            }
          >
            <div className="row">
              {health.data.capabilities.map((capability) => (
                <CapabilityDot key={capability.name} capability={capability} />
              ))}
            </div>
            {health.data.capabilities.some((capability) => capability.status !== "ok") ? (
              <p className="subtle" style={{ marginTop: "0.5rem" }}>
                Hover a capability for detail. A degraded model means deterministic extraction
                still runs — your profile is built from what code can read for certain.
              </p>
            ) : null}
          </Card>
        ) : health.error ? (
          <ErrorBox error={health.error} onRetry={health.reload} />
        ) : (
          <Loading label="Checking capabilities" />
        )}
      </div>

      {candidateId ? (
        <div style={{ marginTop: "1.1rem" }}>
          <Card
            title="How the agent labels information"
            actions={
              <button className="btn-sm" onClick={() => onNavigate("profile")}>
                Review profile
              </button>
            }
          >
            <TrustLegend />
            <p className="subtle" style={{ marginTop: "0.5rem" }}>
              A claim is only used in a resume or an application when it is a verified fact or
              something you provided yourself.
            </p>
          </Card>
        </div>
      ) : null}
    </>
  );
}
