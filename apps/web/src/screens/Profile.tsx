/**
 * Candidate Profile — canonical facts, their evidence, and the review loop.
 *
 * Every fact shows the verbatim quote that supports it. That is the point of the
 * screen: the user should be able to check the agent's reading of their own resume
 * without opening the PDF, and confirm or correct it in one place.
 */

import { useState } from "react";

import { api, type CandidateFact, type IngestionReport } from "../lib/api";
import { useAction, useAsync } from "../lib/hooks";
import { Card, EmptyState, ErrorBox, Evidence, Loading, TrustBadge } from "../components/ui";

const CATEGORY_ORDER = [
  "identity",
  "contact",
  "summary",
  "experience",
  "achievement",
  "skill",
  "project",
  "education",
  "certification",
  "publication",
  "language",
  "work_authorization",
  "compensation",
  "availability",
  "preference",
];

export function Profile({ candidateId }: { candidateId: string }) {
  const profile = useAsync(() => api.profile(candidateId), [candidateId]);
  const { run, pending, error: actionError } = useAction();
  const [report, setReport] = useState<IngestionReport | undefined>(undefined);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [filter, setFilter] = useState<"all" | "needs_review">("all");

  const upload = (file: File) =>
    run(
      async () => {
        const result = await api.uploadResume(candidateId, file);
        setReport(result);
      },
      profile.reload,
    );

  if (profile.loading) return <Loading label="Loading profile" />;
  if (profile.error) return <ErrorBox error={profile.error} onRetry={profile.reload} />;
  if (!profile.data) return null;

  const facts = profile.data.facts;
  const shown = filter === "all" ? facts : facts.filter((fact) => !fact.verified);
  const grouped = new Map<string, CandidateFact[]>();
  for (const fact of shown) {
    const list = grouped.get(fact.category) ?? [];
    list.push(fact);
    grouped.set(fact.category, list);
  }
  const categories = [...grouped.keys()].sort(
    (left, right) => CATEGORY_ORDER.indexOf(left) - CATEGORY_ORDER.indexOf(right),
  );
  const unverified = facts.filter((fact) => !fact.verified).length;

  return (
    <>
      <header className="page-head">
        <h1>{profile.data.display_name ?? "Candidate profile"}</h1>
        <p>
          {facts.length} canonical facts, each traceable to your own words.{" "}
          {unverified > 0
            ? `${unverified} still need your confirmation before the agent will use them.`
            : "All facts are confirmed."}
        </p>
      </header>

      {actionError ? <ErrorBox error={actionError} /> : null}

      <Card title="Import a resume">
        <p className="subtle">
          A <code>.tex</code> source enables tailoring; a PDF still builds the profile. The
          original is stored read-only and never modified.
        </p>
        <input
          type="file"
          accept=".pdf,.tex,.latex,.txt,.md"
          disabled={pending}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
            event.target.value = "";
          }}
          style={{ marginTop: "0.5rem" }}
        />
        {pending ? <p className="subtle">Extracting and validating…</p> : null}

        {report ? (
          <div style={{ marginTop: "0.8rem" }} className="stack">
            <div className="row">
              <span className="badge badge-ok">{report.facts_created} facts created</span>
              <span className="badge badge-neutral">{report.evidence_count} evidence records</span>
              {report.facts_rejected > 0 ? (
                <span className="badge badge-danger">{report.facts_rejected} rejected</span>
              ) : null}
              {!report.llm_extraction_ran ? (
                <span className="badge badge-warn">deterministic extraction only</span>
              ) : null}
            </div>
            {report.findings.length > 0 ? (
              <details>
                <summary className="subtle">{report.findings.length} findings</summary>
                <ul className="subtle">
                  {report.findings.slice(0, 12).map((finding, index) => (
                    <li key={`${finding.code}-${index}`}>
                      <strong>{finding.severity}</strong> · {finding.code} — {finding.message}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card
        title="Canonical facts"
        actions={
          <>
            <button
              className={`btn-sm ${filter === "all" ? "btn-primary" : ""}`}
              onClick={() => setFilter("all")}
            >
              All ({facts.length})
            </button>
            <button
              className={`btn-sm ${filter === "needs_review" ? "btn-primary" : ""}`}
              onClick={() => setFilter("needs_review")}
            >
              Needs review ({unverified})
            </button>
          </>
        }
      >
        {facts.length === 0 ? (
          <EmptyState
            title="No facts yet"
            body="Import a resume above. Contact details and skills are extracted deterministically, so this works even with no model configured."
          />
        ) : shown.length === 0 ? (
          <EmptyState title="Nothing to review" body="Every fact has been confirmed." />
        ) : (
          <div className="stack">
            {categories.map((category) => (
              <div key={category}>
                <h3 className="subtle" style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>
                  {category.replace(/_/g, " ")}
                </h3>
                <div className="list" style={{ marginTop: "0.35rem" }}>
                  {(grouped.get(category) ?? []).map((fact) => (
                    <div key={fact.id} className="item" style={{ cursor: "default" }}>
                      <div className="between">
                        <div style={{ flex: 1 }}>
                          {editing === fact.id ? (
                            <div className="stack">
                              <textarea
                                value={draft}
                                onChange={(event) => setDraft(event.target.value)}
                                rows={2}
                              />
                              <div className="row">
                                <button
                                  className="btn-sm btn-primary"
                                  disabled={pending || !draft.trim()}
                                  onClick={() =>
                                    void run(
                                      () => api.correctFact(candidateId, fact.id, draft),
                                      () => {
                                        setEditing(null);
                                        profile.reload();
                                      },
                                    )
                                  }
                                >
                                  Save correction
                                </button>
                                <button className="btn-sm btn-ghost" onClick={() => setEditing(null)}>
                                  Cancel
                                </button>
                              </div>
                              <p className="field-hint">
                                A correction is stored as your own statement, not as something
                                your resume said.
                              </p>
                            </div>
                          ) : (
                            <>
                              <div>{fact.claim}</div>
                              {fact.evidence.slice(0, 1).map((ref) => (
                                <Evidence
                                  key={ref.evidence_id}
                                  quote={ref.quote}
                                  locator={ref.locator}
                                />
                              ))}
                              {fact.evidence.length === 0 ? (
                                <p className="subtle">No supporting text — confirm it to use it.</p>
                              ) : null}
                            </>
                          )}
                        </div>
                        {editing === fact.id ? null : (
                          <div className="stack" style={{ alignItems: "flex-end", gap: "0.35rem" }}>
                            <TrustBadge provenance={fact.provenance} verified={fact.verified} />
                            <div className="row" style={{ gap: "0.3rem" }}>
                              {!fact.verified ? (
                                <button
                                  className="btn-sm"
                                  disabled={pending}
                                  onClick={() =>
                                    void run(
                                      () => api.verifyFact(candidateId, fact.id),
                                      profile.reload,
                                    )
                                  }
                                >
                                  Confirm
                                </button>
                              ) : null}
                              <button
                                className="btn-sm btn-ghost"
                                onClick={() => {
                                  setEditing(fact.id);
                                  setDraft(fact.claim);
                                }}
                              >
                                Correct
                              </button>
                              <button
                                className="btn-sm btn-ghost"
                                disabled={pending}
                                onClick={() =>
                                  void run(
                                    () => api.deleteFact(candidateId, fact.id),
                                    profile.reload,
                                  )
                                }
                              >
                                Remove
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Preferences candidateId={candidateId} profile={profile.data} onSaved={profile.reload} />
    </>
  );
}

function Preferences({
  candidateId,
  profile,
  onSaved,
}: {
  candidateId: string;
  profile: { preferences: import("../lib/api").CandidatePreferences };
  onSaved: () => void;
}) {
  const { run, pending, error } = useAction();
  const [titles, setTitles] = useState(
    profile.preferences.target_roles.map((role) => role.title).join(", "),
  );
  const [locations, setLocations] = useState(profile.preferences.locations.join(", "));
  const [exclusions, setExclusions] = useState(profile.preferences.exclusions.join(", "));
  const [minSalary, setMinSalary] = useState(String(profile.preferences.min_salary ?? ""));

  const split = (value: string) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

  return (
    <Card title="Target roles and preferences">
      <p className="subtle">
        What you want, kept separate from what your resume proves. Preferences steer
        discovery and scoring; they never become facts about you.
      </p>
      {error ? <ErrorBox error={error} /> : null}
      <div className="grid grid-2" style={{ marginTop: "0.7rem" }}>
        <div className="field">
          <label htmlFor="titles">Target titles</label>
          <input id="titles" value={titles} onChange={(event) => setTitles(event.target.value)} />
          <div className="field-hint">Comma separated</div>
        </div>
        <div className="field">
          <label htmlFor="locations">Locations</label>
          <input
            id="locations"
            value={locations}
            onChange={(event) => setLocations(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="exclusions">Exclusions</label>
          <input
            id="exclusions"
            value={exclusions}
            onChange={(event) => setExclusions(event.target.value)}
          />
          <div className="field-hint">Industries or companies to rule out entirely</div>
        </div>
        <div className="field">
          <label htmlFor="salary">Minimum salary</label>
          <input
            id="salary"
            type="number"
            min={0}
            value={minSalary}
            onChange={(event) => setMinSalary(event.target.value)}
          />
          <div className="field-hint">Blank means unstated — jobs are never rejected for it</div>
        </div>
      </div>
      <button
        className="btn-primary"
        disabled={pending}
        onClick={() =>
          void run(
            () =>
              api.updatePreferences(candidateId, {
                ...profile.preferences,
                target_roles: split(titles).map((title) => ({
                  title,
                  seniority: null,
                  keywords: [],
                })),
                locations: split(locations),
                exclusions: split(exclusions),
                min_salary: minSalary ? Number(minSalary) : null,
              }),
            onSaved,
          )
        }
      >
        Save preferences
      </button>
    </Card>
  );
}
