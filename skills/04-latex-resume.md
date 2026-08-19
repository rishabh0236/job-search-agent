# LaTeX Resume
Treat LaTeX as source code. Preserve template/macros/formatting. Keep original immutable.

Preferred:
LaTeX -> parser/section detector -> Resume AST -> edit operations -> deterministic patcher -> compile -> validation.

Edit:
operation, target_id, old_text, new_text, evidence_refs, rationale, confidence.

Before patch: verify exact old_text and unique target.
After patch: compile, extract PDF text, compare page count/markers, flag layout changes.

Do not allow unconstrained whole-file rewriting.