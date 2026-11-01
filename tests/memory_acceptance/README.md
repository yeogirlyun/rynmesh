# Independent memory acceptance

These are Codex-authored behavioral oracles developed independently of Gemini implementation. Only the original machine-specific default checkout path was replaced with the repository containing these tests. Assertions and synthetic fixtures are preserved. External model networking is forbidden.

Run from the repository root: `python -m pytest tests/memory_acceptance --basetemp=work/mem-test`. Use a short temporary path on Windows because archive fixtures add encrypted blob names. Real CLI acceptance is separately documented in the delivery report and does not run as part of this suite.
