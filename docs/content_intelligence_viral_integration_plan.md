# Content Intelligence and Viral Analyzer Integration Plan

## Goal

Connect `content_intelligence.generate_content_plan` and
`viral_analyzer.analyze_viral_potential` to the generation workflow without
making video creation slower, brittle, or surprising by default.

## Phase 1: Visible Preflight, No Blocking

- Add an optional "Analyze Topic" action in WebUI before generation.
- Run content intelligence from the current subject, script, language, and
  target platform.
- Show the resulting hook ideas, audience, structure, risks, and suggested
  search angles in a compact panel.
- Do not modify the prompt or stop generation in this phase.

Verification:
- Unit test the wrapper that normalizes empty or malformed analysis output.
- WebUI helper test for rendering a minimal analysis result.

## Phase 2: Post-Generation Viral Review

- Keep the existing optional viral analysis after video generation.
- Add material attributions and social metadata to the viral analysis context.
- Store the resulting score and warnings in history.
- Surface the top 2 or 3 actionable warnings next to generated videos.

Verification:
- Task/history test that saved runs include viral analysis when enabled.
- Fallback test when the LLM analyzer is unavailable.

## Phase 3: Optional Quality Gate

- Add a disabled-by-default setting such as `viral_quality_gate_enabled`.
- If enabled, warn when the score is below a configurable threshold.
- The first version should warn only; it should not block generation.
- Keep a "continue anyway" path in the UI.

Verification:
- Unit tests for threshold behavior.
- WebUI helper test for warning text.

## Phase 4: Assisted Rewrite

- Add an explicit "Improve Script" action that uses the content plan and viral
  warnings to propose a revised script.
- Do not auto-rewrite silently.
- Preserve the original script in history for comparison.

Verification:
- Unit test prompt construction.
- Regression test that original script is not overwritten unless the user
  accepts the rewrite.

## Recommended Order

1. Phase 1 preflight panel.
2. Phase 2 richer post-generation viral review.
3. Phase 3 warning-only quality gate.
4. Phase 4 assisted rewrite.

## Non-Goals

- No automatic generation blocking in the first implementation.
- No hidden prompt rewriting.
- No new external dependencies unless an existing service cannot cover the need.
