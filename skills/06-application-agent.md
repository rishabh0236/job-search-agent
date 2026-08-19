# Application Agent
State machine:
CREATED -> PREPARING -> READY_FOR_REVIEW -> USER_APPROVED -> SUBMITTING -> SUBMITTED
Alternate: VERIFICATION_REQUIRED / FAILED / STOPPED.

Workflow:
discover form -> map fields -> fill safe fields -> ask unknowns -> validate -> review -> explicit user confirmation -> submit -> verify -> record.

Stop on CAPTCHA, unexpected authentication, suspicious page, payment request or unknown high-impact question. Never bypass controls.