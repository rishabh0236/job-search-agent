# Local DevOps
Keep the local stack small.

Start with SQLite. Add PostgreSQL only when concurrent access or query needs justify it.
Suggested commands:
make dev
make test
make lint
make format
make seed
make mock-site

Use .env.example for names only. Keep secrets in .env/OS secret store. Store local data under data/raw, data/processed, data/resumes, data/applications, data/browser, data/logs.