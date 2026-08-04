# User-Centered Job Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent accidental reruns of existing jobs and make every job state expose one clear, safe next action.

**Architecture:** Add a small pure decision module that derives available actions and user-facing guidance from a job snapshot. The GUI consumes that decision instead of inferring button states ad hoc, while the CLI independently rejects `worker-start` for completed jobs so the safety boundary cannot be bypassed.

**Tech Stack:** Python 3.11, Tkinter/ttk, Pydantic job state, pytest

---

### Task 1: Lock the interaction contract

**Files:**
- Create: `src/aicf/job_actions.py`
- Create: `tests/test_job_actions.py`

- [ ] Add failing tests for new-job, completed, recoverable-failure, interrupted, running, and missing-video states.
- [ ] Run `python -m pytest tests/test_job_actions.py -q` and confirm the module is missing.
- [ ] Implement `JobActionState` and `derive_job_actions`.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Protect the backend entry point

**Files:**
- Modify: `src/aicf/cli.py`
- Modify: `tests/test_cli_and_logging.py`

- [ ] Add a failing test proving `worker-start` does not launch a completed job.
- [ ] Reject completed jobs before constructing `WorkerLauncher`, returning a clear JSON result.
- [ ] Run the focused CLI test and related worker tests.

### Task 3: Separate new and historical jobs

**Files:**
- Modify: `src/aicf/gui.py`
- Modify: `tests/test_gui_settings.py`

- [ ] Add a visible `新建任务` action that clears historical selection and generates a fresh ID.
- [ ] Reject `开始生成` when the ID already exists and direct the user to `继续/恢复` or `新建任务`.
- [ ] Derive start, resume, stop, and open-video button states from `derive_job_actions`.
- [ ] Show state-specific next-step guidance in the status bar.

### Task 4: Keep dialogs visible

**Files:**
- Modify: `src/aicf/settings_dialog.py`
- Modify: `src/aicf/gui.py`

- [ ] Bring settings and model dialogs to the foreground after creation.
- [ ] Preserve modal ownership and unsaved-change protection.

### Task 5: Verify and publish

**Files:**
- Modify: `pyproject.toml`

- [ ] Run focused job-action, CLI, worker, and GUI-setting tests.
- [ ] Repeat forward and reverse desktop interaction checks.
- [ ] Run `scripts/test.ps1` and require coverage at or above 80%.
- [ ] Commit only source, tests, plan, and coverage configuration.
- [ ] Push `main` without force.
