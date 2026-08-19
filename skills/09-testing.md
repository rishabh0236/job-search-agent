# Testing
Unit: PDF parsing, LaTeX targets, patching, normalization, dedupe, scoring, schema validation.
Golden: fixed candidate + JD -> expected evidence and bounded score.
Safety: hallucinated metrics, unsupported skills, contradictory dates, unknown authorization, CAPTCHA, duplicate-submit.
Integration: mocked ATS and local HTML forms.
Every discovered bug becomes a regression test.