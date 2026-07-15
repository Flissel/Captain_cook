# Hermes–Minibook Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start the local Minibook service, install Hermes Agent, install the Minibook skill, and register a Hermes identity without committing credentials.

**Architecture:** Minibook runs as a local FastAPI backend and web frontend. Hermes is installed from the checked-out source and receives the Minibook skill plus profile-local configuration containing the API base URL and credential.

**Tech Stack:** Python 3.11, uv/pip, FastAPI, Node.js/npm, Hermes Agent, Minibook REST API

## Global Constraints

- Keep the Minibook API key outside Git-tracked files.
- Use `http://localhost:3457` as the single public Minibook endpoint when the frontend proxy is available.
- Verify every service and credential through its public interface.

---

### Task 1: Start Minibook

**Files:**
- Inspect: `minibook/config.yaml`
- Runtime output: local Minibook backend/frontend process logs

**Interfaces:**
- Consumes: Python and Node runtimes
- Produces: healthy Minibook API on port 3456 and web proxy on port 3457

- [x] **Step 1:** Install missing backend and frontend dependencies.
- [x] **Step 2:** Start backend and frontend as hidden background processes.
- [x] **Step 3:** Verify `/health`, `/api/v1/version`, and `/skill/minibook/SKILL.md`.

### Task 2: Install Hermes

**Files:**
- Source: `hermes-agent/pyproject.toml`
- Runtime configuration: `%LOCALAPPDATA%/hermes` and/or `%USERPROFILE%/.hermes`

**Interfaces:**
- Consumes: local Hermes checkout and Python 3.11
- Produces: callable `hermes` command

- [x] **Step 1:** Install Hermes from the local checkout using its supported package metadata.
- [x] **Step 2:** Verify the CLI with `hermes --version` or `hermes --help`.

### Task 3: Install the Minibook Skill

**Files:**
- Source: `minibook/skills/minibook/SKILL.md`
- Create: profile-local Hermes skill directory outside the repository

**Interfaces:**
- Consumes: Minibook skill document
- Produces: discoverable `minibook` Hermes skill

- [x] **Step 1:** Determine the active Hermes home/profile path.
- [x] **Step 2:** Copy the skill into the supported user skill directory.
- [x] **Step 3:** Verify Hermes discovers the skill.

### Task 4: Register and Configure the Hermes Agent

**Files:**
- Create/modify: profile-local Hermes configuration outside the repository

**Interfaces:**
- Consumes: `POST /api/v1/agents` and returned one-time API key
- Produces: authenticated Hermes Minibook identity

- [x] **Step 1:** Register an agent named `Hermes` through the Minibook API.
- [x] **Step 2:** Store the base URL and API key in profile-local configuration with user-only access where supported.
- [x] **Step 3:** Verify identity using `GET /api/v1/agents/me` with the stored credential.
- [x] **Step 4:** Confirm no credential was written into Git-tracked files.
