# Candidate Intelligence
Turn resume PDF/text + LaTeX + user preferences into an evidence-grounded canonical candidate model.

Pipeline:
extract -> parse -> normalize -> evidence-link -> validate -> user review.

Fact categories: identity, contact, summary, experience, achievement, skill, project, education, publication, certification, language, preference, work_authorization, compensation, availability.

Every claim needs evidence_ref + confidence. Unsupported claims are UNKNOWN. Semantic similarity is not evidence. Keep candidate preferences separate from resume facts.