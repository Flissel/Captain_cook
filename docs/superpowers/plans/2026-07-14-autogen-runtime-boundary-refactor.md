# AutoGen Runtime Boundary Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the current runtime use AutoGen 0.7.5 APIs, isolate legacy behavior, and make AgentFarm imports testable without credentials or network access.

**Architecture:** Keep the domain/event/ledger code independent from AutoGen. Use `autogen_agentchat.AssistantAgent` behind the workflow boundary and preserve the existing synchronous public workflow API through an async implementation. Treat AgentFarm's LLM clients as lazy infrastructure dependencies instead of import-time globals.

**Tech Stack:** Python 3.11, AutoGen Core/AgentChat/Ext 0.7.5, pytest, asyncio, existing Pydantic models.

## Global Constraints

- AutoGen target versions remain `autogen-core==0.7.5`, `autogen-agentchat==0.7.5`, and `autogen-ext[openai]==0.7.5`.
- No production import may require the legacy `pyautogen` distribution.
- Importing a package or test module must not create an LLM client or require an API key.
- Preserve existing workflow names and synchronous callers while adding async execution internally.
- Do not change the event/ledger business rules in this refactor.

---

### Task 1: Replace legacy nested-chat execution with an AgentChat workflow runner

**Files:**
- Modify: `agenten/workflows/base.py`
- Modify: `agenten/workflows/system_prompt.py`
- Modify: `agenten/workflows/subtask_extraction.py`
- Create: `tests/workflows/test_base.py`

**Interfaces:**
- `NestedChatWorkflow.run_async(captain, context=None) -> Coroutine[Any, Any, str]` executes one kickoff plus the declared steps.
- `NestedChatWorkflow.run(captain, context=None) -> str` remains the synchronous compatibility entry point.
- A workflow agent is any object with `async run(*, task: str)` returning a result with a final message content.

- [ ] **Step 1: Write the failing tests**

  Add a fake captain and fake async agents. Assert that kickoff output is passed to later steps, callable reflection messages receive `history` and `context`, and the synchronous wrapper rejects calls from an already-running event loop.

- [ ] **Step 2: Run the focused tests**

  Run: `python -m pytest tests/workflows/test_base.py -q`

  Expected: FAIL because `run_async` and the new execution path do not exist.

- [ ] **Step 3: Implement the minimal runner**

  Replace `register_nested_chats`, `initiate_chat`, and old callback objects with sequential `AssistantAgent.run`-compatible calls. Render each step with accumulated outputs, call each step `max_turns` times, extract the final string content, and retain `result_index` selection.

- [ ] **Step 4: Update reflection workflows**

  Change `reflection_message` and `update_message` to accept `history` and `context`. Set `subtask_extraction` to return the final generated output under the new history model.

- [ ] **Step 5: Run the focused tests again**

  Run: `python -m pytest tests/workflows/test_base.py -q`

  Expected: PASS.

---

### Task 2: Move Captain and auxiliary components off `from autogen import ...`

**Files:**
- Modify: `agenten/Captain.py`
- Modify: `agenten/critic.py`
- Modify: `blockchain/web_scamler.py`
- Modify: `tests/test_import_boundaries.py`

**Interfaces:**
- `CaptainAgent(..., model_client=None)` accepts an injected current AutoGen `ChatCompletionClient` for tests.
- Existing `create_agent_assistant`, `create_agent_user_proxy`, and `run_workflow` names remain available.
- `NestedChatForURLEvaluation.setup_agents()` remains callable but no longer constructs legacy AutoGen agents.

- [ ] **Step 1: Add import-boundary tests**

  Assert `agenten.Captain`, `agenten.critic`, and `blockchain.web_scamler` import with no `autogen` 0.2 dependency and assert that `CaptainAgent` can create an agent with a fake injected model client.

- [ ] **Step 2: Run the tests to verify the old boundary fails**

  Run: `python -m pytest tests/test_import_boundaries.py -q`

  Expected: FAIL on the legacy imports and/or constructor signatures.

- [ ] **Step 3: Implement the current AgentChat boundary**

  Use `autogen_agentchat.agents.AssistantAgent`, lazily build the current model client from the existing `llm_config`, make the user-proxy method an explicit compatibility alias to an assistant role, and lazy-load the heavyweight internet-search tool.

- [ ] **Step 4: Remove dead legacy imports from URL evaluation**

  Replace `new_struct.*` imports with local `agenten.functions.*` imports and keep URL evaluation as a plain async service because the created agents were never used by the evaluator.

- [ ] **Step 5: Run focused and root tests**

  Run: `python -m pytest tests/test_import_boundaries.py tests/workflows/test_base.py -q` and then `python -m pytest tests -q`.

  Expected: new tests pass; only unrelated pre-existing failures may remain and must be investigated before continuing.

---

### Task 3: Make AgentFarm imports lazy and test scopes explicit

**Files:**
- Modify: `Autogen_AgentFarm/minibook/swarm/constants.py`
- Modify: `Autogen_AgentFarm/minibook/swarm/llm.py`
- Modify: `Autogen_AgentFarm/minibook/swarm/__init__.py`
- Create: `Autogen_AgentFarm/pytest.ini`
- Create: `Autogen_AgentFarm/tests/test_import_safety.py`

**Interfaces:**
- `get_openai_client()` and `get_anthropic_client()` return cached clients and raise only when the corresponding provider is actually used.
- Importing `swarm`, `swarm.input_parser`, and `minibook.swarm.knowledge` does not instantiate an SDK client.
- AgentFarm tests run from the AgentFarm repository root with `pythonpath = minibook`.

- [ ] **Step 1: Write the failing import-safety tests**

  Clear provider keys, import the package in a subprocess, and assert no `OpenAIError` occurs and the client cache is empty until a getter is called.

- [ ] **Step 2: Run the tests to verify the import side effect**

  Run: `python -m pytest Autogen_AgentFarm/tests/test_import_safety.py -q`

  Expected: FAIL because `constants.py` constructs `AsyncOpenAI()` during import and `swarm/__init__.py` eagerly imports the entire pipeline.

- [ ] **Step 3: Implement lazy client factories**

  Store client caches as `None`, add provider getters, and replace direct `openai_client`/`anthropic_client` calls in `swarm/llm.py` with getter calls.

- [ ] **Step 4: Reduce package import side effects**

  Keep `swarm/__init__.py` limited to package metadata and lightweight constants; callers import concrete modules explicitly.

- [ ] **Step 5: Add AgentFarm pytest configuration and verify**

  Run: `cd Autogen_AgentFarm; python -m pytest tests/test_import_safety.py -q`

  Expected: PASS without an API key or running Minibook server.

---

### Task 4: Align documentation and dependency boundaries

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `requirements.txt`
- Modify: `Autogen_AgentFarm/minibook/requirements.txt`
- Modify: `Autogen_AgentFarm/docs/readme_agentfarm.md`
- Modify: `Autogen_AgentFarm/README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Document the two repositories and the supported AutoGen API**

  Mark the old Captain/NestedChat files as compatibility code, state that the runtime target is AutoGen 0.7.5, and remove claims that no dependency file exists.

- [ ] **Step 2: Separate dependency manifests**

  Add the dependencies used by the AgentFarm swarm runner to its own manifest without mixing them into the Minibook backend-only manifest.

- [ ] **Step 3: Ignore generated graph, cache, and runtime artifacts**

  Add `graphify-out/`, `test_claude_output.txt`, and relevant generated AgentFarm state paths to the correct repository ignore files.

- [ ] **Step 4: Run the final verification**

  Run root tests, AgentFarm import-safety tests, and `python -m compileall -q agenten blockchain Autogen_AgentFarm/minibook`.

  Expected: all refactor tests pass; any remaining failures are reported with their exact pre-existing cause.

