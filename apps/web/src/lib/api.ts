/**
 * Typed API client.
 *
 * Two deliberate choices:
 *
 * - Domain errors from the backend arrive as a structured `{error: {...}}` body.
 *   `ApiError` preserves `code`, `details` and the safety-stop fields, because the UI
 *   renders a safety stop as an interrupt with an explanation, not a toast.
 * - No data-fetching library. The screens are simple reads plus explicit actions, so
 *   a small `useAsync` hook covers it without adding a dependency (or a cache whose
 *   staleness could show an approved resume that no longer exists).
 */

const BASE = import.meta.env.DEV ? "/api" : "";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly reason?: string;
  readonly requiresUserAction: boolean;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    reason?: string,
    requiresUserAction = false,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.reason = reason;
    this.requiresUserAction = requiresUserAction;
  }

  /** True when the backend refused because a human has to act. */
  get isSafetyStop(): boolean {
    return this.code === "safety_stop";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers:
        init?.body instanceof FormData
          ? init?.headers
          : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new ApiError(0, "network_error", "The API is unreachable. Is `make dev` running?");
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? (JSON.parse(text) as unknown) : null;

  if (!response.ok) {
    const body = payload as { error?: Record<string, unknown>; detail?: unknown } | null;
    const error = body?.error;
    if (error) {
      throw new ApiError(
        response.status,
        String(error.code ?? "error"),
        String(error.message ?? "Request failed"),
        (error.details as Record<string, unknown>) ?? {},
        error.reason as string | undefined,
        Boolean(error.requires_user_action),
      );
    }
    // FastAPI validation errors have a different shape.
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => (item as { msg?: string }).msg ?? "invalid input").join("; ")
      : String(detail ?? `Request failed (${response.status})`);
    throw new ApiError(response.status, "validation_failed", message);
  }

  return payload as T;
}

const get = <T,>(path: string) => request<T>(path);
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body) });
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
const del = (path: string) => request<void>(path, { method: "DELETE" });

// ------------------------------------------------------------------- types

export type Provenance = "resume" | "user" | "ai_suggestion" | "unknown";

export interface EvidenceRef {
  evidence_id: string;
  source_id: string;
  locator: string;
  quote: string;
}

export interface CandidateFact {
  id: string;
  candidate_id: string;
  category: string;
  claim: string;
  attributes: Record<string, string>;
  evidence: EvidenceRef[];
  confidence: number;
  provenance: Provenance;
  verified: boolean;
}

export interface TargetRole {
  title: string;
  seniority: string | null;
  keywords: string[];
}

export interface CandidatePreferences {
  target_roles: TargetRole[];
  locations: string[];
  remote_modes: string[];
  employment_types: string[];
  min_salary: number | null;
  salary_currency: string | null;
  exclusions: string[];
  willing_to_relocate: boolean | null;
  notice_period_days: number | null;
  work_authorization: Record<string, string>;
}

export interface CandidateProfile {
  id: string;
  display_name: string | null;
  preferences: CandidatePreferences;
  facts: CandidateFact[];
}

export interface IngestionFinding {
  severity: string;
  code: string;
  message: string;
  locator: string | null;
  claim: string | null;
}

export interface IngestionReport {
  resume_id: string;
  candidate_id: string;
  source_type: string;
  sha256: string;
  is_original: boolean;
  block_count: number;
  evidence_count: number;
  facts_created: number;
  facts_needing_review: number;
  facts_rejected: number;
  sections: string[];
  findings: IngestionFinding[];
  llm_extraction_ran: boolean;
}

export interface JobRequirement {
  text: string;
  kind: "required" | "preferred" | "contextual";
  key: string | null;
}

export interface Job {
  id: string;
  source: string;
  source_job_id: string;
  company: string;
  title: string;
  location: string | null;
  remote: string;
  employment_type: string;
  description: string;
  requirements: JobRequirement[];
  salary: { min_amount: number | null; max_amount: number | null; currency: string | null } | null;
  url: string | null;
  retrieved_at: string;
  dedupe_group: string | null;
  raw: Record<string, unknown>;
}

export interface ScoreComponent {
  name: string;
  raw_score: number;
  weight: number;
  rationale: string;
}

export interface MatchedRequirement {
  requirement: string;
  satisfied: boolean | null;
  evidence: EvidenceRef[];
  note: string;
}

export interface JobMatch {
  id: string;
  job_id: string;
  candidate_id: string;
  score: number;
  eligibility: "eligible" | "ineligible" | "unknown";
  hard_constraints: { eligibility: string; blocking: string[]; unknown: string[] };
  components: ScoreComponent[];
  strengths: MatchedRequirement[];
  gaps: MatchedRequirement[];
  explanation: string;
  uncertainty: string[];
  weights_used: Record<string, number>;
}

export interface MatchListResponse {
  matches: JobMatch[];
  jobs: Record<string, Job>;
}

export interface ValidationFinding {
  severity: string;
  code: string;
  message: string;
  target_id: string | null;
}

export interface TailoringResult {
  resume_id: string;
  job_id: string;
  mode: string;
  edits: {
    id: string;
    target_id: string;
    old_text: string;
    new_text: string;
    rationale: string;
    confidence: number;
    status: string;
  }[];
  compile_result: {
    success: boolean;
    pdf_path: string | null;
    log_excerpt: string;
    page_count: number | null;
    duration_ms: number;
    engine: string;
  } | null;
  findings: ValidationFinding[];
}

export interface ApplicationAnswer {
  id: string;
  application_id: string;
  field: string;
  question: string;
  answer: string;
  source: Provenance;
  confidence: number;
  user_verified: boolean;
  sensitive: boolean;
}

export interface Application {
  id: string;
  candidate_id: string;
  job_id: string;
  status: string;
  approved_resume_id: string | null;
  cover_letter_artifact_id: string | null;
  answers: ApplicationAnswer[];
  submitted_at: string | null;
  confirmation_ref: string | null;
  idempotency_key: string | null;
  stop_reason: string | null;
  stop_detail: string;
}

export interface Capability {
  name: string;
  status: "ok" | "degraded" | "unavailable";
  detail: string;
}

export interface Health {
  status: "ok" | "degraded" | "unavailable";
  app_env: string;
  version: string;
  capabilities: Capability[];
}

export interface SourceHealth {
  source: string;
  healthy: boolean;
  detail: string | null;
  checked_at: string;
}

export interface DiffEntry {
  target_id: string;
  before: string;
  after: string;
  rationale: string;
  status: string;
}

// ------------------------------------------------------------------- calls

export const api = {
  health: () => get<Health>("/health"),

  createCandidate: (displayName: string) =>
    post<{ id: string }>("/candidates", { display_name: displayName }),
  profile: (candidateId: string) => get<CandidateProfile>(`/candidates/${candidateId}`),
  updatePreferences: (candidateId: string, preferences: CandidatePreferences) =>
    put<CandidateProfile>(`/candidates/${candidateId}/preferences`, preferences),

  uploadResume: (candidateId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<IngestionReport>(`/candidates/${candidateId}/resumes`, {
      method: "POST",
      body: form,
    });
  },
  verifyFact: (candidateId: string, factId: string) =>
    post<CandidateFact>(`/candidates/${candidateId}/facts/${factId}/verify`),
  correctFact: (candidateId: string, factId: string, claim: string) =>
    patch<CandidateFact>(`/candidates/${candidateId}/facts/${factId}`, { claim }),
  addFact: (candidateId: string, category: string, claim: string) =>
    post<CandidateFact>(`/candidates/${candidateId}/facts`, { category, claim, attributes: {} }),
  deleteFact: (candidateId: string, factId: string) =>
    del(`/candidates/${candidateId}/facts/${factId}`),

  sources: () => get<SourceHealth[]>("/jobs/sources"),
  discover: (titles: string[], locations: string[]) =>
    post<{ stored: number; unique_postings: number; jobs: Job[] }>("/jobs/discover", {
      criteria: { titles, locations, limit: 50 },
    }),
  jobs: () => get<Job[]>("/jobs"),
  job: (jobId: string) => get<Job>(`/jobs/${jobId}`),
  duplicates: (jobId: string) => get<Job[]>(`/jobs/${jobId}/duplicates`),

  computeMatches: (candidateId: string) =>
    post<MatchListResponse>("/matches", { candidate_id: candidateId, explain: false }),
  matches: (candidateId: string) =>
    get<MatchListResponse>(`/candidates/${candidateId}/matches`),
  match: (candidateId: string, jobId: string) =>
    get<JobMatch>(`/candidates/${candidateId}/matches/${jobId}`),
  explainMatch: (candidateId: string, jobId: string) =>
    post<JobMatch>(`/candidates/${candidateId}/matches/${jobId}/explain`),

  tailor: (candidateId: string, resumeId: string, jobId: string, mode: string) =>
    post<TailoringResult>("/resumes/tailor", {
      candidate_id: candidateId,
      resume_id: resumeId,
      job_id: jobId,
      mode,
    }),
  diff: (resumeId: string) => get<DiffEntry[]>(`/resumes/${resumeId}/diff`),

  applications: (candidateId: string) =>
    get<Application[]>(`/candidates/${candidateId}/applications`),
  application: (applicationId: string) => get<Application>(`/applications/${applicationId}`),
  createApplication: (candidateId: string, jobId: string) =>
    post<Application>("/applications", { candidate_id: candidateId, job_id: jobId }),
  setAnswer: (applicationId: string, field: string, answer: string) =>
    put<Application>(`/applications/${applicationId}/answers`, { field, answer }),
  checklist: (applicationId: string) =>
    get<{ ready: boolean; blockers: string[] }>(`/applications/${applicationId}/checklist`),
  approve: (applicationId: string) =>
    post<Application>(`/applications/${applicationId}/approve`),
};
