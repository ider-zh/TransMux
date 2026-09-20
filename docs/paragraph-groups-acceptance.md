# Paragraph grouping acceptance — 2026-09-17

Implemented adjacent body paragraph merges/splits with ordered source coverage, protected structural boundaries, grouped comparison/revision, immutable published versions, and DOCX export. Source/target evidence IDs are scoped to their document version. Long windows carry the provisional final group into the next window; no extra per-paragraph agent workflow.

## Validation

- Full regression run: 123 passed; one legacy schema assertion failed because it required exactly one output per input. Updated that test to the grouped protocol and bounded retry behavior; all four encoded-output cases passed on rerun.
- Added grouping tests: 13 passed, covering protected boundaries, export, selective revision, rejected coverage, terminology evidence IDs and cross-window merging without duplicate coverage or stale terminology.
- Combined grouping and terminology verification after the final backend changes: 28 passed.
- Existing browser smoke: passed, including comparison revisions and downloads.
- Grouped comparison browser acceptance: desktop/mobile display, merge/split counts, reasons and version switching passed.
- OfficeCLI validated the actual translated DOCX from both agents with no errors.
- Ruff and JavaScript syntax checks passed.

## Real CLI acceptance

Both tests used synthetic Chinese documents in separate disposable application stores, with RAG disabled to isolate translation and paragraph handling. Each agent translated, reviewed, exported and then revised a merged group back to two paragraphs. The fixture requires an accidental sentence-break merge, a style-directed split, and preservation of heading/list/table boundaries. Original source and prior published version were byte-for-byte unchanged. These are focused acceptance cases, not a guarantee for every document or model.

- **codex / gpt-5.6-luna**: passed. Artifacts: `/home/ider/workspace/TransMux/build/groups-codex-_a13iz7a`.
- **codebuddy / hy3**: passed. Artifacts: `/home/ider/workspace/TransMux/build/groups-codebuddy-6vm77i5_`.

Reproduce (these commands call the configured third-party model providers):

```sh
rtk proxy env PYTHONPATH=. python3 scripts/test_live_groups.py codex --model gpt-5.6-luna
rtk proxy env PYTHONPATH=. python3 scripts/test_live_groups.py codebuddy --model hy3
```

PDF/TXT protection is limited to structure recoverable from extracted text. DOCX structural styles, objects, table containers, section/page breaks and blank paragraph boundaries receive conservative protection. Existing one-to-one comparison records remain readable; existing translated files are not rewritten automatically.
