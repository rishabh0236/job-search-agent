# Job Discovery
Implement a JobSource adapter:
search(criteria), fetch(job_id), normalize(raw_job), health_check().

Start with a small number of permitted/public ATS or company sources. Do not build universal scraping first.

Implemented (`services/jobs/sources.py`): LocalFixtureSource (offline default),
GreenhouseSource, LeverSource, AshbySource, SmartRecruitersSource (per-company job
board APIs, board tokens configured per source), AdzunaSource (aggregator, free
self-serve key, has an India region), ArbeitnowSource (aggregator, no key, opt-in),
CareerPageSource (arbitrary company career pages, read via embedded schema.org
JobPosting structured data — the same feed Google/Bing job search consumes, not
DOM scraping; robots.txt is checked for every request including followed links).
LinkedIn/Naukri/Wellfound have no public/self-serve jobs-search API and no such
structured-data feed either — only an enterprise partner program — so they are
out of scope for every adapter here.

Normalize: source, source_job_id, company, title, location, remote, employment_type, description, requirements, preferred, salary, url, posted_at, retrieved_at.

Deduplicate using source IDs, requisition IDs, canonical URLs, normalized company/title/location and description similarity. Store provenance.