/**
 * Job Discovery and Job Detail.
 *
 * The feed collapses duplicate postings and shows why a job scored what it did. The
 * detail screen is where the score is broken down component by component — a number
 * with no explanation is not useful, and the backend guarantees the components sum
 * to it.
 */

import { useState } from "react";

import { api, type Job, type JobMatch } from "../lib/api";
import { useAction, useAsync } from "../lib/hooks";
import {
  Card,
  EmptyState,
  ErrorBox,
  Evidence,
  Loading,
  ScoreMeter,
  StatusPill,
} from "../components/ui";

interface FeedProps {
  candidateId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}

export function JobFeed({ candidateId, onNavigate }: FeedProps) {
  const sources = useAsync(() => api.sources(), []);
  const matches = useAsync(() => api.matches(candidateId), [candidateId]);
  const jobs = useAsync(() => api.jobs(), []);
  const { run, pending, error } = useAction();
  const [titles, setTitles] = useState("");
  const [locations, setLocations] = useState("");
  const [hideIneligible, setHideIneligible] = useState(false);

  const reloadAll = () => {
    matches.reload();
    jobs.reload();
  };

  const scored = new Map((matches.data?.matches ?? []).map((match) => [match.job_id, match]));
  const jobsById = new Map((jobs.data ?? []).map((job) => [job.id, job]));
  const ranked = [...(jobs.data ?? [])].sort(
    (left, right) => (scored.get(right.id)?.score ?? -1) - (scored.get(left.id)?.score ?? -1),
  );
  const visible = hideIneligible
    ? ranked.filter((job) => scored.get(job.id)?.eligibility !== "ineligible")
    : ranked;

  return (
    <>
      <header className="page-head">
        <h1>Job discovery</h1>
        <p>
          Jobs from your configured sources, deduplicated across boards and ranked against your
          profile. Postings are treated as untrusted text throughout.
        </p>
      </header>

      {error ? <ErrorBox error={error} /> : null}

      <Card
        title="Sources"
        actions={
          <button className="btn-sm" onClick={() => sources.reload()}>
            Recheck
          </button>
        }
      >
        {sources.loading ? (
          <Loading label="Checking sources" />
        ) : (
          <div className="row">
            {(sources.data ?? []).map((source) => (
              <span
                key={source.source}
                className={`badge ${source.healthy ? "badge-ok" : "badge-danger"}`}
                title={source.detail ?? ""}
              >
                <span className="badge-dot" aria-hidden="true" />
                {source.source}
              </span>
            ))}
          </div>
        )}
      </Card>

      <Card title="Search">
        <div className="grid grid-2">
          <div className="field">
            <label htmlFor="job-titles">Titles</label>
            <input
              id="job-titles"
              placeholder="Senior Machine Learning Engineer"
              value={titles}
              onChange={(event) => setTitles(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="job-locations">Locations</label>
            <input
              id="job-locations"
              placeholder="Bengaluru, Remote"
              value={locations}
              onChange={(event) => setLocations(event.target.value)}
            />
          </div>
        </div>
        <div className="row">
          <button
            className="btn-primary"
            disabled={pending}
            onClick={() =>
              void run(
                () =>
                  api.discover(
                    titles.split(",").map((item) => item.trim()).filter(Boolean),
                    locations.split(",").map((item) => item.trim()).filter(Boolean),
                  ),
                reloadAll,
              )
            }
          >
            {pending ? "Discovering…" : "Discover jobs"}
          </button>
          <button
            disabled={pending}
            onClick={() => void run(() => api.computeMatches(candidateId), reloadAll)}
          >
            Score against my profile
          </button>
        </div>
      </Card>

      <Card
        title={`Feed (${visible.length})`}
        actions={
          <label className="row" style={{ margin: 0, fontWeight: 500 }}>
            <input
              type="checkbox"
              checked={hideIneligible}
              onChange={(event) => setHideIneligible(event.target.checked)}
              style={{ width: "auto" }}
            />
            Hide ineligible
          </label>
        }
      >
        {jobs.loading ? (
          <Loading label="Loading jobs" />
        ) : visible.length === 0 ? (
          <EmptyState
            title="No jobs stored yet"
            body="Run a discovery pass above. The local fixture source works offline; configure a job source (Greenhouse, Lever, Ashby, SmartRecruiters, Adzuna, Arbeitnow or a company career page) in .env for real postings."
          />
        ) : (
          <div className="list">
            {visible.map((job) => {
              const match = scored.get(job.id);
              return (
                <button key={job.id} className="item" onClick={() => onNavigate("job", { id: job.id })}>
                  <div className="between">
                    <div style={{ flex: 1 }}>
                      <div className="item-title">{job.title}</div>
                      <div className="subtle">
                        {job.company} · {job.location ?? "location not stated"} · {job.remote}
                      </div>
                      <div className="row" style={{ marginTop: "0.35rem", gap: "0.3rem" }}>
                        <span className="badge badge-neutral">{job.source}</span>
                        {match ? <StatusPill status={match.eligibility} /> : (
                          <span className="badge badge-neutral">not scored</span>
                        )}
                        {job.requirements.length > 0 ? (
                          <span className="badge badge-neutral">
                            {job.requirements.filter((item) => item.kind === "required").length} required
                          </span>
                        ) : null}
                      </div>
                    </div>
                    {match ? <ScoreMeter score={match.score} /> : null}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </Card>

      {jobsById.size > 0 && matches.data && matches.data.matches.length === 0 ? (
        <p className="subtle">Jobs are stored but unscored. Use “Score against my profile”.</p>
      ) : null}
    </>
  );
}

// ------------------------------------------------------------------- detail

interface DetailProps {
  candidateId: string;
  jobId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}

export function JobDetail({ candidateId, jobId, onNavigate }: DetailProps) {
  const job = useAsync(() => api.job(jobId), [jobId]);
  const duplicates = useAsync(() => api.duplicates(jobId), [jobId]);
  const match = useAsync(
    () => api.match(candidateId, jobId).catch(() => undefined),
    [candidateId, jobId],
  );
  const { run, pending, error } = useAction();

  if (job.loading) return <Loading label="Loading job" />;
  if (job.error) return <ErrorBox error={job.error} onRetry={job.reload} />;
  if (!job.data) return null;

  const suspicious = job.data.raw?.suspicious_instructions as string[] | undefined;

  return (
    <>
      <header className="page-head">
        <button className="btn-sm btn-ghost" onClick={() => onNavigate("jobs")}>
          ← Back to feed
        </button>
        <h1 style={{ marginTop: "0.4rem" }}>{job.data.title}</h1>
        <p>
          {job.data.company} · {job.data.location ?? "location not stated"} · {job.data.remote} ·{" "}
          {job.data.employment_type.replace(/_/g, " ")}
          {job.data.url ? (
            <>
              {" · "}
              <a href={job.data.url} target="_blank" rel="noreferrer noopener">
                original posting
              </a>
            </>
          ) : null}
        </p>
      </header>

      {error ? <ErrorBox error={error} /> : null}

      {suspicious && suspicious.length > 0 ? (
        <div className="callout callout-warn" style={{ marginBottom: "0.85rem" }}>
          <div>
            <strong>This posting contains text aimed at automated screening</strong>
            It was ignored and excluded from the requirements below, but you should know it is
            there.
            <ul className="subtle" style={{ marginTop: "0.4rem" }}>
              {suspicious.slice(0, 3).map((line) => (
                <li key={line}>
                  <code>{line.slice(0, 160)}</code>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {match.data ? (
        <MatchPanel
          match={match.data}
          onExplain={() =>
            void run(() => api.explainMatch(candidateId, jobId), match.reload)
          }
          explaining={pending}
        />
      ) : (
        <Card title="Not scored yet">
          <button
            className="btn-primary"
            disabled={pending}
            onClick={() => void run(() => api.computeMatches(candidateId), match.reload)}
          >
            Score this job
          </button>
        </Card>
      )}

      <Card
        title="Requirements"
        badge={<span className="badge badge-neutral">{job.data.requirements.length}</span>}
      >
        {job.data.requirements.length === 0 ? (
          <p className="subtle">
            No requirements could be extracted, so the score for this job is weakly grounded.
          </p>
        ) : (
          <div className="stack">
            {(["required", "preferred", "contextual"] as const).map((kind) => {
              const items = job.data!.requirements.filter((item) => item.kind === kind);
              if (items.length === 0) return null;
              return (
                <div key={kind}>
                  <h3 className="subtle" style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>
                    {kind}
                  </h3>
                  <ul>
                    {items.map((item, index) => (
                      <li key={`${kind}-${index}`}>
                        {item.text}
                        {item.key ? <span className="mono"> · {item.key}</span> : null}
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card title="Posting" actions={
        <button className="btn-sm btn-primary" onClick={() => onNavigate("tailor", { id: jobId })}>
          Tailor resume for this job
        </button>
      }>
        <pre
          style={{
            whiteSpace: "pre-wrap",
            fontFamily: "inherit",
            margin: 0,
            maxHeight: "22rem",
            overflow: "auto",
          }}
        >
          {job.data.description}
        </pre>
      </Card>

      {(duplicates.data ?? []).length > 0 ? (
        <Card title={`Also posted elsewhere (${duplicates.data!.length})`}>
          <div className="list">
            {duplicates.data!.map((duplicate: Job) => (
              <div key={duplicate.id} className="item" style={{ cursor: "default" }}>
                <div className="subtle">
                  {duplicate.source} · {duplicate.source_job_id} · {duplicate.title}
                </div>
              </div>
            ))}
          </div>
        </Card>
      ) : null}
    </>
  );
}

function MatchPanel({
  match,
  onExplain,
  explaining,
}: {
  match: JobMatch;
  onExplain: () => void;
  explaining: boolean;
}) {
  return (
    <Card
      title="Match analysis"
      badge={<StatusPill status={match.eligibility} />}
      actions={
        <button className="btn-sm" disabled={explaining} onClick={onExplain}>
          {explaining ? "Explaining…" : match.explanation ? "Re-explain" : "Explain this match"}
        </button>
      }
    >
      <div className="between" style={{ marginBottom: "0.7rem" }}>
        <ScoreMeter score={match.score} />
        <span className="subtle">
          {match.components.length} weighted components · weights sum to{" "}
          {Object.values(match.weights_used).reduce((sum, value) => sum + value, 0).toFixed(2)}
        </span>
      </div>

      {match.explanation ? (
        <div className="callout callout-info" style={{ marginBottom: "0.7rem" }}>
          <div>
            <strong>AI explanation</strong>
            {match.explanation}
          </div>
        </div>
      ) : null}

      {match.hard_constraints.blocking.length > 0 ? (
        <div className="callout callout-stop" style={{ marginBottom: "0.7rem" }}>
          <div>
            <strong>Blocking constraints</strong>
            <ul>
              {match.hard_constraints.blocking.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {match.uncertainty.length > 0 ? (
        <div className="callout callout-warn" style={{ marginBottom: "0.7rem" }}>
          <div>
            <strong>Needs confirmation — not counted against you</strong>
            <ul>
              {match.uncertainty.slice(0, 6).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      <div className="grid grid-2">
        <div>
          <h3>Strengths</h3>
          {match.strengths.length === 0 ? (
            <p className="subtle">No requirements were matched to evidence.</p>
          ) : (
            <div className="stack" style={{ marginTop: "0.35rem" }}>
              {match.strengths.map((item, index) => (
                <div key={`${item.requirement}-${index}`}>
                  <div>
                    <span className="badge badge-ok">met</span> {item.requirement}
                  </div>
                  {item.evidence.slice(0, 1).map((ref) => (
                    <Evidence key={ref.evidence_id} quote={ref.quote} locator={ref.locator} />
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h3>Gaps</h3>
          {match.gaps.length === 0 ? (
            <p className="subtle">Nothing missing.</p>
          ) : (
            <div className="stack" style={{ marginTop: "0.35rem" }}>
              {match.gaps.map((item, index) => (
                <div key={`${item.requirement}-${index}`}>
                  <span className={`badge ${item.satisfied === null ? "badge-warn" : "badge-danger"}`}>
                    {item.satisfied === null ? "unclear" : "missing"}
                  </span>{" "}
                  {item.requirement}
                  {item.note ? <div className="subtle">{item.note}</div> : null}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <details style={{ marginTop: "0.8rem" }}>
        <summary className="subtle">Score breakdown</summary>
        <dl className="kv" style={{ marginTop: "0.5rem" }}>
          {match.components.map((component) => (
            <div key={component.name} style={{ display: "contents" }}>
              <dt>{component.name.replace(/_/g, " ")}</dt>
              <dd>
                <span className="score">{(component.raw_score * component.weight).toFixed(3)}</span>{" "}
                <span className="subtle">
                  ({component.raw_score.toFixed(2)} × {component.weight.toFixed(2)}) —{" "}
                  {component.rationale}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </Card>
  );
}
