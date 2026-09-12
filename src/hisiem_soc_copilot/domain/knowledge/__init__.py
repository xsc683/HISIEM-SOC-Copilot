"""Knowledge bounded context (supporting domain).

Versioned security knowledge: what a trusted source said, in which immutable
version, and what that version hashes to. This is a SUPPORTING context
(brief P3-A section 2):

- it never enters the Investigation aggregate;
- it never authorizes, approves, executes, changes tenant, or creates a Verdict;
- retrieved content is DATA_ONLY material, never instructions.

Pure stdlib only -- no framework imports (python-package-boundary.md section 3).
"""
