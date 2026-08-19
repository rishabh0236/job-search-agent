/**
 * Resume Tailoring and the diff review.
 *
 * The screen's job is to make a rejected edit as visible as an accepted one. A run
 * that produced no new version because a proposal invented a metric is a *success*
 * of the guardrails, and the UI says so rather than reporting a bare failure.
 */

import { useState } from "react";

import { api, type TailoringResult } from "../lib/api";
import { useAction, useAsync } from "../lib/hooks";
import { Card, EmptyState, ErrorBox, Loading } from "../components/ui";

const MODES = [
  { id: "conservative", label: "Conservative", note: "Wording and terminology only" },
  { id: "balanced", label: "Balanced", note: "Rewrite phrasing, keep every fact" },
  { id: "aggressive", label: "Aggressive", note: "Restructure emphasis, facts still fixed" },
];

interface Props {
  candidateId: string;
  jobId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}

export function Tailor({ candidateId, jobId, onNavigate }: Props) {
  const job = useAsync(() => api.job(jobId), [jobId]);
  const profile = useAsync(() => api.profile(candidateId), [candidateId]);
  const { run, pending, error } = useAction();
  const [mode, setMode] = useState("balanced");
  const [result, setResult] = useState<TailoringResult | undefined>(undefined);
  const [resumeId, setResumeId] = useState("");

  if (job.loading || profile.loading) return <Loading label="Loading" />;
  if (job.error) return <ErrorBox error={job.error} onRetry={job.reload} />;

  const errors = (result?.findings ?? []).filter((finding) => finding.severity === "error");
  const warnings = (result?.findings ?? []).filter((finding) => finding.severity === "warning");
  const notes = (result?.findings ?? []).filter((finding) => finding.severity === "info");
  const applied = (result?.edits ?? []).filter((edit) => edit.status === "applied");
  const rejected = (result?.edits ?? []).filter((edit) => edit.status === "rejected");
  const blocked = errors.length > 0 || result?.compile_result?.success === false;

  return (
    <>
      <header className="page-head">
        <button className="btn-sm btn-ghost" onClick={() => onNavigate("job", { id: jobId })}>
          ← Back to job
        </button>
        <h1 style={{ marginTop: "0.4rem" }}>Tailor resume</h1>
        <p>
          {job.data?.title} at {job.data?.company}. Your original is never modified — a
          successful run produces a new version, compiled and checked against the original PDF.
        </p>
      </header>

      {error ? <ErrorBox error={error} /> : null}

      <Card title="Run">
        <div className="field">
          <label htmlFor="resume-id">Source resume id</label>
          <input
            id="resume-id"
            placeholder="res_… (the .tex version to tailor from)"
            value={resumeId}
            onChange={(event) => setResumeId(event.target.value)}
          />
          <div className="field-hint">
            Shown in the ingestion report after you import a <code>.tex</code> file.
          </div>
        </div>

        <div className="row" style={{ marginBottom: "0.7rem" }}>
          {MODES.map((option) => (
            <button
              key={option.id}
              className={`btn-sm ${mode === option.id ? "btn-primary" : ""}`}
              onClick={() => setMode(option.id)}
              title={option.note}
            >
              {option.label}
            </button>
          ))}
        </div>
        <p className="subtle">{MODES.find((option) => option.id === mode)?.note}</p>

        <button
          className="btn-primary"
          disabled={pending || !resumeId.trim()}
          onClick={() =>
            void run(async () => {
              const outcome = await api.tailor(candidateId, resumeId.trim(), jobId, mode);
              setResult(outcome);
            })
          }
          style={{ marginTop: "0.6rem" }}
        >
          {pending ? "Tailoring and compiling…" : "Propose edits"}
        </button>
      </Card>

      {result ? (
        <>
          <Card
            title="Outcome"
            badge={
              blocked ? (
                <span className="badge badge-danger">blocked</span>
              ) : (
                <span className="badge badge-ok">new version created</span>
              )
            }
          >
            <div className="row" style={{ marginBottom: "0.6rem" }}>
              <span className="badge badge-ok">{applied.length} applied</span>
              {rejected.length > 0 ? (
                <span className="badge badge-danger">{rejected.length} rejected</span>
              ) : null}
              {result.compile_result ? (
                <span
                  className={`badge ${result.compile_result.success ? "badge-ok" : "badge-danger"}`}
                >
                  compile {result.compile_result.success ? "ok" : "failed"}
                  {result.compile_result.page_count
                    ? ` · ${result.compile_result.page_count}p`
                    : ""}
                </span>
              ) : null}
              {!blocked ? <span className="mono">{result.resume_id}</span> : null}
            </div>

            {blocked ? (
              <div className="callout callout-stop">
                <div>
                  <strong>No new version was created — and that is the guardrail working</strong>
                  An edit failed validation, so nothing was written. Your original is untouched.
                  <ul style={{ marginTop: "0.4rem" }}>
                    {errors.map((finding, index) => (
                      <li key={`${finding.code}-${index}`}>
                        <code>{finding.code}</code> — {finding.message}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : (
              <div className="callout callout-ok">
                <div>
                  <strong>Version compiled and verified</strong>
                  Attach <code>{result.resume_id}</code> to an application when you are happy
                  with the diff below.
                </div>
              </div>
            )}

            {warnings.length > 0 ? (
              <div className="callout callout-warn" style={{ marginTop: "0.6rem" }}>
                <div>
                  <strong>Warnings</strong>
                  <ul>
                    {warnings.map((finding, index) => (
                      <li key={`${finding.code}-${index}`}>{finding.message}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}

            {notes.length > 0 ? (
              <details style={{ marginTop: "0.6rem" }}>
                <summary className="subtle">
                  {notes.length} requirements your resume does not evidence
                </summary>
                <ul className="subtle">
                  {notes.map((finding, index) => (
                    <li key={`${finding.code}-${index}`}>{finding.message}</li>
                  ))}
                </ul>
                <p className="subtle">
                  These are shown rather than filled in. Add a fact on your profile if one is
                  genuinely true.
                </p>
              </details>
            ) : null}
          </Card>

          <Card title="Proposed changes">
            {result.edits.length === 0 ? (
              <EmptyState
                title="No edits proposed"
                body="Either the resume already suits this posting, or no model is configured to propose changes."
              />
            ) : (
              <div className="stack">
                {result.edits.map((edit) => (
                  <div key={edit.id} className="item" style={{ cursor: "default" }}>
                    <div className="between" style={{ marginBottom: "0.4rem" }}>
                      <span className="mono">{edit.target_id}</span>
                      <span
                        className={`badge ${edit.status === "applied" ? "badge-ok" : "badge-danger"}`}
                      >
                        {edit.status}
                      </span>
                    </div>
                    <div className="diff">
                      <div className="diff-side diff-before">
                        <h4>Before</h4>
                        {edit.old_text}
                      </div>
                      <div className="diff-side diff-after">
                        <h4>After</h4>
                        {edit.new_text}
                      </div>
                    </div>
                    {edit.rationale ? (
                      <p className="subtle" style={{ marginTop: "0.4rem" }}>
                        {edit.rationale}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      ) : null}
    </>
  );
}
