/**
 * Application Review, the Apply Runner state, and the Tracker.
 *
 * The review screen is the safety-critical surface of the whole product. Two rules
 * shape it:
 *
 * - Submit is not offered until the checklist is clear. The button is genuinely
 *   absent, not disabled with a tooltip, so there is nothing to click past.
 * - Every answer carries its provenance badge, and anything the model wrote or that
 *   touches salary/authorization is presented as a question to you, not an answer.
 */

import { useState } from "react";

import { api, type Application, type ApplicationAnswer } from "../lib/api";
import { useAction, useAsync } from "../lib/hooks";
import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  StatusPill,
  TrustBadge,
} from "../components/ui";

// ------------------------------------------------------------------ tracker

interface TrackerProps {
  candidateId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}

export function ApplicationTracker({ candidateId, onNavigate }: TrackerProps) {
  const applications = useAsync(() => api.applications(candidateId), [candidateId]);
  const jobs = useAsync(() => api.jobs(), []);

  if (applications.loading) return <Loading label="Loading applications" />;
  if (applications.error) return <ErrorBox error={applications.error} onRetry={applications.reload} />;

  const jobsById = new Map((jobs.data ?? []).map((job) => [job.id, job]));
  const rows = applications.data ?? [];

  return (
    <>
      <header className="page-head">
        <h1>Applications</h1>
        <p>
          Every application, its state, and what it is waiting on. Nothing reaches an employer
          without your explicit approval.
        </p>
      </header>

      {rows.length === 0 ? (
        <EmptyState
          title="No applications yet"
          body="Open a job, tailor your resume for it, then start an application from the job detail screen."
          action={
            <button className="btn-primary" onClick={() => onNavigate("jobs")}>
              Browse jobs
            </button>
          }
        />
      ) : (
        <Card title={`${rows.length} applications`}>
          <div className="list">
            {rows.map((application) => {
              const job = jobsById.get(application.job_id);
              const pending = application.answers.filter(
                (answer) => answer.sensitive || answer.source === "ai_suggestion" || answer.source === "unknown",
              ).filter((answer) => !answer.user_verified).length;
              return (
                <button
                  key={application.id}
                  className="item"
                  onClick={() => onNavigate("review", { id: application.id })}
                >
                  <div className="between">
                    <div>
                      <div className="item-title">{job?.title ?? application.job_id}</div>
                      <div className="subtle">
                        {job?.company ?? "unknown company"}
                        {application.confirmation_ref ? ` · ref ${application.confirmation_ref}` : ""}
                      </div>
                      {application.stop_reason ? (
                        <div className="subtle" style={{ color: "var(--danger)" }}>
                          stopped: {application.stop_reason.replace(/_/g, " ")}
                        </div>
                      ) : null}
                    </div>
                    <div className="row">
                      {pending > 0 ? (
                        <span className="badge badge-warn">{pending} to confirm</span>
                      ) : null}
                      <StatusPill status={application.status} />
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </Card>
      )}
    </>
  );
}

// ------------------------------------------------------------------- review

interface ReviewProps {
  applicationId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}

export function ApplicationReview({ applicationId, onNavigate }: ReviewProps) {
  const application = useAsync(() => api.application(applicationId), [applicationId]);
  const checklist = useAsync(() => api.checklist(applicationId), [applicationId]);
  const { run, pending, error } = useAction();

  const reload = () => {
    application.reload();
    checklist.reload();
  };

  if (application.loading) return <Loading label="Loading application" />;
  if (application.error) return <ErrorBox error={application.error} onRetry={application.reload} />;
  if (!application.data) return null;

  const data = application.data;
  const ready = checklist.data?.ready ?? false;
  const blockers = checklist.data?.blockers ?? [];
  const needsUser = data.answers.filter(
    (answer) =>
      !answer.user_verified &&
      (answer.sensitive || answer.source === "ai_suggestion" || answer.source === "unknown"),
  );
  const settled = data.answers.filter((answer) => !needsUser.includes(answer));
  const approved = data.status === "user_approved" || data.status === "submitted";

  return (
    <>
      <header className="page-head">
        <button className="btn-sm btn-ghost" onClick={() => onNavigate("applications")}>
          ← All applications
        </button>
        <h1 style={{ marginTop: "0.4rem" }}>Application review</h1>
        <div className="row">
          <StatusPill status={data.status} />
          <span className="mono">{data.id}</span>
          {data.approved_resume_id ? (
            <span className="badge badge-neutral">resume {data.approved_resume_id.slice(0, 12)}…</span>
          ) : (
            <span className="badge badge-danger">no resume attached</span>
          )}
        </div>
      </header>

      {error ? <ErrorBox error={error} /> : null}

      {data.stop_reason ? (
        <div className="callout callout-stop" style={{ marginBottom: "0.85rem" }}>
          <div>
            <strong>This run stopped: {data.stop_reason.replace(/_/g, " ")}</strong>
            {data.stop_detail}
          </div>
        </div>
      ) : null}

      {data.status === "submitted" ? (
        <div className="callout callout-ok" style={{ marginBottom: "0.85rem" }}>
          <div>
            <strong>Submitted</strong>
            {data.confirmation_ref
              ? `Confirmation reference ${data.confirmation_ref}.`
              : "No confirmation reference was found on the page — check the site."}
          </div>
        </div>
      ) : null}

      {needsUser.length > 0 ? (
        <Card
          title={`Needs your answer (${needsUser.length})`}
          badge={<span className="badge badge-warn">required before approval</span>}
        >
          <p className="subtle">
            Salary, work authorization, notice period and anything the model drafted are always
            yours to answer. The agent will not guess these.
          </p>
          <div className="stack" style={{ marginTop: "0.6rem" }}>
            {needsUser.map((answer) => (
              <AnswerEditor
                key={answer.id}
                answer={answer}
                disabled={pending || approved}
                onSave={(value) =>
                  void run(() => api.setAnswer(applicationId, answer.field, value), reload)
                }
              />
            ))}
          </div>
        </Card>
      ) : null}

      <Card title={`Confirmed answers (${settled.length})`}>
        {settled.length === 0 ? (
          <p className="subtle">Nothing filled in yet.</p>
        ) : (
          <dl className="kv">
            {settled.map((answer) => (
              <div key={answer.id} style={{ display: "contents" }}>
                <dt>{answer.question || answer.field}</dt>
                <dd className="row" style={{ justifyContent: "space-between" }}>
                  <span>{answer.answer || <span className="subtle">— empty —</span>}</span>
                  <TrustBadge provenance={answer.source} verified={answer.user_verified} />
                </dd>
              </div>
            ))}
          </dl>
        )}
      </Card>

      <Card title="Pre-submit checklist">
        {checklist.loading ? (
          <Loading label="Checking" />
        ) : ready ? (
          <div className="callout callout-ok">
            <div>
              <strong>Everything is resolved</strong>
              You can approve this application. Approval is required before anything is
              submitted, and it can only be given once.
            </div>
          </div>
        ) : (
          <div className="callout callout-warn">
            <div>
              <strong>{blockers.length} item(s) outstanding</strong>
              <ul>
                {blockers.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <div className="row" style={{ marginTop: "0.8rem" }}>
          {/* Submit is absent, not merely disabled, until the checklist is clear. */}
          {ready && !approved ? (
            <button
              className="btn-primary"
              disabled={pending}
              onClick={() => void run(() => api.approve(applicationId), reload)}
            >
              Approve this application
            </button>
          ) : null}
          {approved ? (
            <span className="badge badge-ok">approved by you</span>
          ) : null}
          <button className="btn-sm btn-ghost" onClick={reload}>
            Refresh
          </button>
        </div>

        {approved && data.status !== "submitted" ? (
          <p className="subtle" style={{ marginTop: "0.6rem" }}>
            Submission runs from the CLI runner against the target site, and only when
            <code> CA_ALLOW_BROWSER_SUBMIT=true</code>. That switch is separate from your
            approval on purpose: both must be true.
          </p>
        ) : null}
      </Card>
    </>
  );
}

function AnswerEditor({
  answer,
  disabled,
  onSave,
}: {
  answer: ApplicationAnswer;
  disabled: boolean;
  onSave: (value: string) => void;
}) {
  const [value, setValue] = useState(answer.answer);

  return (
    <div className="item" style={{ cursor: "default" }}>
      <div className="between" style={{ marginBottom: "0.35rem" }}>
        <label htmlFor={answer.id} style={{ margin: 0 }}>
          {answer.question || answer.field}
        </label>
        <div className="row" style={{ gap: "0.3rem" }}>
          {answer.sensitive ? <span className="badge badge-unknown">sensitive</span> : null}
          <TrustBadge provenance={answer.source} verified={answer.user_verified} />
        </div>
      </div>
      <div className="row">
        <input
          id={answer.id}
          value={value}
          disabled={disabled}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Your answer"
        />
        <button
          className="btn-sm btn-primary"
          disabled={disabled || !value.trim()}
          onClick={() => onSave(value)}
        >
          Save
        </button>
      </div>
      {answer.source === "ai_suggestion" && answer.answer ? (
        <p className="field-hint">
          Drafted by the model. Saving it records the answer as yours.
        </p>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------- starter

export function StartApplication({
  candidateId,
  jobId,
  onNavigate,
}: {
  candidateId: string;
  jobId: string;
  onNavigate: (screen: string, params?: Record<string, string>) => void;
}) {
  const { run, pending, error } = useAction();
  const [created, setCreated] = useState<Application | undefined>(undefined);

  return (
    <Card title="Application">
      {error ? <ErrorBox error={error} /> : null}
      {created ? (
        <button className="btn-primary" onClick={() => onNavigate("review", { id: created.id })}>
          Open review
        </button>
      ) : (
        <button
          className="btn-primary"
          disabled={pending}
          onClick={() =>
            void run(async () => {
              setCreated(await api.createApplication(candidateId, jobId));
            })
          }
        >
          {pending ? "Creating…" : "Start an application"}
        </button>
      )}
    </Card>
  );
}
