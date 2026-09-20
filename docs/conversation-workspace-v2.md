# TransMux conversation workspaces v2

The new application starts with independent, empty workspaces. It does not migrate or query v1 data.

## Run and access

- Entrypoint: `transmux-v2` or `python3 -m uvicorn transmux.v2:app --host 127.0.0.1 --port 8765`.
- Storage: `TRANSMUX_V2_DATA`, default `data-v2/`; v1 remains in `data/`.
- LAN: http://192.168.1.220:8765
- Tailnet: http://100.64.156.74:8765 or http://lambdax220.tailed8a37.ts.net:8765
- User services: `transmux-v2.service`, `transmux-tailnet-v2.service`.
- V2 now owns port 8765. The original app and its Tailnet service are stopped; original data is retained. Port 8766 is no longer served.
- Historical branch: `archive/v1-2026-09-20` (initial commit `ee9be42`). Development branch: `feat/conversation-workspace-v2`.

## Interaction and task boundaries

Each workspace contains one conversation, an immutable agent and target language, and an adjustable model. English is the creation default; Simplified Chinese is also available. The scheduler pins the model when a task starts, serializes work within a workspace, and allows different workspaces to run concurrently.

The three columns are workspace selection, conversation/composer, and organized files/preview. Work events stream through SSE. Details are initially collapsed; elapsed time, the current phase, failures and cancellation remain accessible. Artifact links open the right-hand preview. The preview pane expands, the tree can collapse, and the composer remains at the bottom while conversation content scrolls.

Four templates set an explicit task type and a short editable starter prompt. Maintained `transmux/skills/*/SKILL.md` instructions are loaded on the server; the exact skill is saved with task artifacts. Uploaded files are data, not additional instructions. Only attached/selected files are processed; an attachment can be marked as a glossary rather than a source document.

- **Style:** explicitly selected documents join the reference corpus. Per-document evidence and bounded extraction batches are cached. Synthesis uses the maintained reference corpus and separate user requirements, not the previous automatically generated guide. Reference deletion excludes its observations on the next synthesis. Language validation and configuration history remain in effect. Changes to task instructions or skill content invalidate the corresponding extraction identity.
- **Translation:** uses requirements, learned style, active terms and people, and optional attached glossaries. Chapters take priority, with bounded windows and semantic review. Multi-document tasks preserve separate input/alignment/review records and publish immutable DOCX copies.
- **Typesetting:** a separate task, with original, JCST and IEEE Access presets. Only one selected document is processed; otherwise the newest successful translation is shown and used. Original-format DOCX is byte-preserving. Presets reuse the versioned official-template rules. A bounded citation step can change anchored citation markers, reorder existing bibliography metadata and apply publication-title italics. Missing metadata is flagged, never researched or invented. Ambiguous changes remain unapplied. Publication presets produce review drafts, not a guarantee of submission compliance. Large citation sections exceeding the formatting budget are retained with a review notice.
- **Fact checking:** uses the agent's external search/page tools, reports claim status, sources and evidence, and does not change the input. Without an observed external-tool event or usable sources, findings become insufficient-evidence findings. Reports are agent-generated and require user review; the harness does not independently certify source truth. A bounded repair separates mistakenly quoted website evidence from the input-document anchor. A reply of “是” to the latest completed report creates a new copy using only sufficiently evidenced, uniquely anchored corrections. The report is linked to a hashed source snapshot; stale confirmations do not apply to a different report.

Chat edits apply exact anchored changes only to selected files and publish a new version. Explicit requests can also adjust font, font size, line spacing or one/two columns. Files are not overwritten. Guidance and terminology can be edited directly, with optimistic revision checks and history; terminology has a table and paste-import UI. Other documents are read-only in preview. DOCX previews show reading content; journal PDF previews show the rendered layout. DOC uploads require LibreOffice and retain both the original and converted DOCX. Scanned PDFs still require OCR before upload.

## Runtime

RAG is not initialized, exposed as a task, or invoked in v2. No embedding model is loaded. Structured agent steps use independent CLI sessions; the single conversation history is maintained by the harness. Input estimates are bounded before sending requests. CodeBuddy uses an explicit tool list, no permission bypass, no inherited MCP servers, and a bounded turn count. Only fact checking enables web tools. Codex uses the read-only sandbox, disables inherited host MCP integrations for these tasks, and enables built-in web search for fact checking. The harness applies validated changes and publishes outputs.

Agent capability restrictions do not make the entire host a multi-tenant sandbox. This remains the existing trusted LAN/Tailnet deployment; no public exposure or new authentication model is introduced.

## Validation

- Full pre-final regression: 166 tests passed; subsequent focused v2 suite: 7 tests passed (including real artifact publication paths).
- New tests cover scoped attachments, no RAG, immutable fact-check correction versions, stale report confirmation, evidence fallback, reference enrollment, citation constraints, all three layout presets and chat formatting.
- `tests/browser_v2.py` covers the three columns, attachments, translation, preview, report confirmation, editable history, terminology tables, composer anchoring and responsive width.
- `scripts/test_live_v2.py` exercises real Codex/CodeBuddy with synthetic text, keeping acceptance data under ignored `build/`.
- LAN/Tailnet health and cross-origin rejection were checked; v1 remained healthy and v2 started with zero workspaces.

Real LibreOffice validation generated DOCX/PDF with all three presets and converted a binary DOC fixture back to DOCX under `build/v2-layout-4yjedcae/`.

Live acceptance records: CodeBuddy `hy3` completed translation and web-sourced fact checking in `build/v2-codebuddy-wpob238v/`; Codex `gpt-5.6-luna` completed both in `build/v2-codex-ckpvfr08/`. A later Codex check with inherited host MCP servers disabled still encountered intermittent model-catalog and built-in web transport timeouts; these are surfaced in work details. Agent output is not a substitute for reviewing the cited evidence.

## Agent-specific startup environment

The operator-owned repository `.env` is read for each CLI launch. It is ignored by Git and never sourced as shell code. Set `TRANSMUX_ENV_FILE` to use another explicit configuration path. Do not put this configuration in a document workspace.

```dotenv
TRANSMUX_CODEX_HTTP_PROXY=http://192.168.1.230:10808
```

This supplies lowercase `http_proxy` only to Codex subprocesses, equivalent to exporting it before launching Codex. It does not modify the backend environment or override CodeBuddy's inherited environment. Optional `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` suffixes follow the same mapping; CodeBuddy has a separate `TRANSMUX_CODEBUDDY_` prefix. Agent-specific service environment variables override `.env`; empty values explicitly clear inherited proxy settings. Later `.env` edits apply to the next CLI process without restarting the service. Already running CLI processes keep their original environment.

## Model selection at creation

New Workspace includes a default-model selector. Opening it, switching agents, or clicking Refresh queries the selected CLI afresh. The saved choice becomes the workspace model; the existing header can change it later without changing a running job's pinned model. Racing responses from a previously selected agent cannot replace the current selection.

Codex uses the read-only `app-server` initialization and paginated [`model/list`](https://developers.openai.com/codex/app-server/) protocol; CodeBuddy uses its stream-JSON `initialize` / `get_available_models` control protocol. No prompt, translation task or conversation is started. Each lookup receives that agent's configured proxy environment and has a bounded timeout. Failure falls back to the current local Codex cache or CodeBuddy CLI-declared list with an explicit warning and retry button. Administrator model overrides are identified as configured lists. The app no longer keeps the old indefinite model-list cache; freshness ultimately follows the catalog the installed Agent reports.
