# Reliable Research Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-invented research URLs with discovered, preflighted candidates and make failed research retries use a fresh attempt with clear user-facing recovery.

**Architecture:** A new `research_policy` module owns freshness, evidence thresholds, error categories, attempt identity, and summaries. A separate `source_discovery` module finds and preflights candidate URLs; `ResearchEngine` may only cite those candidates, while `SourceVerifier` remains the authority for URL safety and claim support. The runner persists attempt/rejection metadata, and the GUI exposes a dedicated research retry action.

**Tech Stack:** Python 3.11, Pydantic, urllib, OpenRouter structured generation, SQLite job state, Tkinter/ttk, pytest

---

### Task 1: Research policy and error categories

**Files:**
- Create: `src/aicf/research_policy.py`
- Create: `tests/test_research_policy.py`
- Modify: `src/aicf/source_verifier.py`
- Modify: `tests/test_source_verifier.py`

- [ ] **Step 1: Write failing policy tests**

Add tests proving:

```python
assert classify_source_error("URL HTTP 404") == SourceFailureKind.PERMANENT_SOURCE_FAILURE
assert classify_source_error("URL HTTP 503") == SourceFailureKind.TEMPORARY_SOURCE_FAILURE
assert classify_source_error("claim 关键词支持度不足") == SourceFailureKind.UNSUPPORTED_CLAIM
assert derive_freshness("下半年全球主线", today=date(2026, 8, 6)).cutoff_date == date(2025, 8, 6)
assert ResearchPolicy().accepts(verified=5, total=8, authoritative=1)
```

- [ ] **Step 2: Run focused tests and confirm missing module failures**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research_policy.py tests/test_source_verifier.py -q
```

Expected: failures for missing `research_policy` contracts.

- [ ] **Step 3: Implement immutable policy contracts**

Implement:

```python
class SourceFailureKind(str, Enum):
    PERMANENT_SOURCE_FAILURE = "PERMANENT_SOURCE_FAILURE"
    TEMPORARY_SOURCE_FAILURE = "TEMPORARY_SOURCE_FAILURE"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    INSUFFICIENT_FRESHNESS = "INSUFFICIENT_FRESHNESS"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

@dataclass(frozen=True)
class ResearchPolicy:
    minimum_verified_facts: int = 5
    minimum_verified_ratio: float = 0.60
    minimum_authoritative_sources: int = 1
```

Add `category` to failed evidence in `SourceVerifier` without weakening existing URL safety or keyword support checks.

- [ ] **Step 4: Run focused tests**

Expected: all policy and verifier tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/aicf/research_policy.py src/aicf/source_verifier.py tests/test_research_policy.py tests/test_source_verifier.py
git commit -m "feat(research): classify source failures"
```

### Task 2: Attempt-scoped research cache

**Files:**
- Modify: `src/aicf/engines/research_engine.py`
- Modify: `src/aicf/m2_runner.py`
- Modify: `tests/test_m2_content_run.py`
- Modify: `tests/test_m2_openrouter.py`

- [ ] **Step 1: Write failing cache-isolation tests**

Prove that:

```python
first_payload["research_attempt_id"] == "attempt-1"
second_payload["research_attempt_id"] == "attempt-2"
```

and that two calls in the same attempt retain the same cache identity while a resumed `FAILED_RETRYABLE/RESEARCHED` job receives a new identity. Assert direction/topic cache inputs remain unchanged.

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_m2_content_run.py tests/test_m2_openrouter.py -q
```

- [ ] **Step 3: Persist attempt metadata**

Write `outputs/<job>/research_attempt.json`:

```json
{
  "attempt_id": "<uuid hex>",
  "created_at": "<UTC ISO timestamp>",
  "reason": "initial|automatic_retry|user_retry"
}
```

Pass `research_attempt_id` only to research-stage structured calls. A fresh runner invocation after `RESEARCHED` failure creates a new attempt; repair rounds inside that invocation reuse it.

- [ ] **Step 4: Run focused tests**

Expected: cache isolation tests and existing M2 resume tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/aicf/engines/research_engine.py src/aicf/m2_runner.py tests/test_m2_content_run.py tests/test_m2_openrouter.py
git commit -m "fix(research): isolate retry cache attempts"
```

### Task 3: Source discovery and preflight

**Files:**
- Create: `src/aicf/source_discovery.py`
- Create: `tests/test_source_discovery.py`
- Modify: `src/aicf/cli.py`

- [ ] **Step 1: Write failing discovery tests**

Define and test:

```python
candidate = SourceCandidate(
    url="https://www.federalreserve.gov/example",
    title="Federal Reserve example",
    published_at=date(2026, 7, 1),
    source_type="official",
    query="Federal Reserve policy 2026",
)
```

Tests must prove rejected URLs are omitted, 404/410 are never retried, 503 is retried within the existing bound, duplicate canonical URLs collapse, and candidates older than the freshness cutoff are marked non-core.

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_source_discovery.py -q
```

- [ ] **Step 3: Implement provider boundary**

Create:

```python
class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[SourceCandidate]: ...

class SourceDiscovery:
    def discover(
        self,
        *,
        topic: dict[str, object],
        direction: str,
        freshness: FreshnessRequirement,
        rejected_urls: set[str],
    ) -> list[SourceCandidate]: ...
```

The production provider uses a structured OpenRouter query to propose search queries and candidate URLs, then requires `SourceVerifier` preflight before candidates are accepted. It is a replaceable boundary; tests use in-memory candidates.

- [ ] **Step 4: Wire discovery in `build_m2_runner`**

Inject `SourceDiscovery` into `M2ContentRunner`; do not create it inside `ResearchEngine`.

- [ ] **Step 5: Run focused and CLI construction tests**

- [ ] **Step 6: Commit**

```powershell
git add src/aicf/source_discovery.py src/aicf/cli.py tests/test_source_discovery.py
git commit -m "feat(research): discover verified source candidates"
```

### Task 4: Candidate allowlist, freshness, and evidence gate

**Files:**
- Modify: `src/aicf/models/contracts.py`
- Modify: `src/aicf/engines/research_engine.py`
- Modify: `src/aicf/m2_runner.py`
- Modify: `tests/test_m2_content_run.py`
- Modify: `tests/test_m2_content_pipeline.py`

- [ ] **Step 1: Write failing allowlist and quality-gate tests**

Prove that research output with a URL outside `source_candidates` raises a validation error before network verification. Add boundary tests for 4/8 rejected, 5/8 accepted, 5/10 rejected, stale core evidence rejected, and one authoritative source accepted.

- [ ] **Step 2: Extend source metadata compatibly**

Add optional fields to `ResearchFact`:

```python
published_at: date | None = None
source_type: str | None = None
```

Existing `research.json` remains valid.

- [ ] **Step 3: Constrain the model request**

Include `source_candidates`, `freshness_required`, and `cutoff_date` in the research payload. After generation, reject any `source_url` not exactly present in the candidate allowlist.

- [ ] **Step 4: Enforce quality policy**

After verification, count supported facts, ratio, authoritative sources, and freshness. Raise categorized `INSUFFICIENT_EVIDENCE` or `INSUFFICIENT_FRESHNESS` errors and persist evidence.

- [ ] **Step 5: Persist rejections**

Merge permanent failures into `outputs/<job>/research_rejections.json`:

```json
{
  "urls": [
    {"url": "...", "category": "PERMANENT_SOURCE_FAILURE", "reason": "URL HTTP 404"}
  ]
}
```

- [ ] **Step 6: Run all M2 tests**

- [ ] **Step 7: Commit**

```powershell
git add src/aicf/models/contracts.py src/aicf/engines/research_engine.py src/aicf/m2_runner.py tests/test_m2_content_run.py tests/test_m2_content_pipeline.py
git commit -m "feat(research): enforce source evidence policy"
```

### Task 5: User-facing retry and failure details

**Files:**
- Modify: `src/aicf/job_actions.py`
- Modify: `src/aicf/gui.py`
- Modify: `tests/test_job_actions.py`
- Modify: `tests/test_gui_settings.py`

- [ ] **Step 1: Write failing action and summary tests**

For `FAILED_RETRYABLE` at `RESEARCHED`, assert:

```python
actions.primary_action == "retry_research"
actions.guidance == "资料研究失败：8 条资料中 7 个网页不存在，1 条内容无法证明相关说法。"
```

Other failed stages must retain normal resume behavior.

- [ ] **Step 2: Add pure failure-summary formatter**

Read categorized `research_sources.json` and produce counts for permanent failures, temporary failures, unsupported claims, stale evidence, and insufficient total evidence.

- [ ] **Step 3: Add GUI actions**

Show:

- `重新搜索资料` only for retryable `RESEARCHED` failures.
- `查看失败详情` when structured research evidence exists.

`重新搜索资料` invokes the existing detached worker path after reopening the failed stage and writes attempt reason `user_retry`; it must not reset `DIRECTION_LOADED` through `TOPIC_SELECTED`.

- [ ] **Step 4: Run focused GUI and action tests**

- [ ] **Step 5: Commit**

```powershell
git add src/aicf/job_actions.py src/aicf/gui.py tests/test_job_actions.py tests/test_gui_settings.py
git commit -m "feat(gui): add research retry recovery"
```

### Task 6: Regression and task 260806 recovery

**Files:**
- Modify only if tests reveal defects in files from Tasks 1-5.

- [ ] **Step 1: Run the complete suite**

```powershell
.\scripts\test.ps1
```

Expected: all tests pass and coverage remains at least 80%.

- [ ] **Step 2: Verify repository hygiene**

```powershell
git diff --check
git status -sb
```

Ensure local `config/` remains untracked and unstaged.

- [ ] **Step 3: Back up task research metadata**

Copy only `outputs/260806/research*.json` and `data/jobs/260806/status.json` to the temporary working directory before recovery. Do not modify completed direction/topic artifacts.

- [ ] **Step 4: Trigger a fresh research attempt**

Use the new dedicated research retry action or equivalent CLI command. Verify:

- a new `research_attempt_id` is created;
- rejected 404 URLs are not reused;
- `DIRECTION_LOADED` through `TOPIC_SELECTED` timestamps remain unchanged;
- the GUI shows active research or a categorized, understandable failure.

- [ ] **Step 5: Run final regression after live verification**

Re-run focused research tests and check task state.

- [ ] **Step 6: Commit and push**

```powershell
git add <reviewed source and test paths only>
git commit -m "feat(research): make source discovery reliable"
git push origin main
```
