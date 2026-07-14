from autogen import UserProxyAgent, AssistantAgent
from .Internet_searcher import InternetSearcher
from .tools.base import Tool, ToolRegistry
from .tools.internet_search import InternetSearchTool
from .workflows.registry import get_workflow
from blockchain.Blockchain_modell import Blockchain
from blockchain.visualisation import restructure_for_tree, plot_project_tree
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from .functions.Caiptain_functions.task_models import Task, Department
from .functions.Caiptain_functions.clean_json_string import clean_json_string
from .decomposition.budget import DecompositionBudget

if TYPE_CHECKING:  # import only for the type annotation; the real import
    # happens lazily inside build_default_supply_chain_pipeline so this
    # module keeps working when the new-subsystem deps aren't installed.
    from .orchestration.pipeline import SupplyChainPipeline


class CaptainAgent:
    def __init__(self, name, llm_config, system_message="None for yet", blockchain_path="blockchain.json"):
        self.name = name
        self.llm_config = llm_config
        self.system_message = system_message
        self.agents = {}
        self.tools = ToolRegistry()
        self.blockchain = Blockchain(file_path=blockchain_path)

    # --- Blockchain -----------------------------------------------------

    def add_task_to_blockchain(self, task, assigned_agents=None, status="pending", parent_index=None):
        return self.blockchain.add_task_block(task, assigned_agents, status, parent_index)

    def update_task_status_in_blockchain(self, index, status):
        return self.blockchain.update_task_status(index, status)

    # --- Agent creation ---------------------------------------------------

    def create_agent_assistant(self, agent_name, system_message):
        """
        Create an agent with the specified name and system message.
        """
        agent = AssistantAgent(
            name=agent_name,
            llm_config=self.llm_config,
            system_message=system_message,
            is_termination_msg=lambda msg: "approve" in msg["content"].lower(),
        )
        self.agents[agent_name] = agent
        return agent

    def create_agent_user_proxy(self, agent_name, system_message):
        agent = UserProxyAgent(
            name=agent_name,
            code_execution_config=False,
            is_termination_msg=lambda msg: "terminate" in msg["content"].lower() or "approve" in msg["content"].lower(),
        )
        self.agents[agent_name] = agent
        return agent

    # --- Tools --------------------------------------------------------------

    def register_tool(self, tool: Tool, name: Optional[str] = None) -> Tool:
        """Register any Tool implementation for agents on this Captain to use."""
        return self.tools.register(tool, name=name)

    def create_internet_searcher(self, tool_name="internet_searcher"):
        """
        Registers an InternetSearcher as a tool for the Captain Agent.

        Kept for backward compatibility; prefer ``register_tool(InternetSearchTool())``.

        Returns:
            callable: An async function that runs the search-and-score tool.
        """
        tool = self.register_tool(InternetSearchTool(InternetSearcher()), name=tool_name)

        async def internet_search(query):
            return await tool.run(query)

        return internet_search

    # --- Agent-logic workflows -----------------------------------------

    def run_workflow(self, workflow_name: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Run any registered NestedChatWorkflow against this Captain.

        New agent logic is added by registering a workflow (see
        agenten/workflows/*.py) rather than by adding a bespoke method here.
        """
        return get_workflow(workflow_name).run(self, context=context)

    def make_system_prompt(self, task_description):
        """Create a system prompt for a task via the 'system_prompt' workflow."""
        return self.run_workflow("system_prompt", {"task_description": task_description})

    def automate_project_split(self, project_text):
        """
        Automates the process of splitting a project into hierarchical components and subtasks.

        Args:
            project_text (str): The full project description.

        Returns:
            dict: A structured representation of the project with sections, subtasks, and assignments.
        """
        sections = self.extract_sections(project_text)
        project_tree_dict = self.setup_project_structure_nested_chat(project_text)
        return project_tree_dict, sections

    def setup_project_structure_nested_chat(self, project_description):
        """
        Processes a project description into a detailed hierarchical dictionary via the
        'project_structuring' workflow, then renders and parses the result.

        Args:
            project_description (str): The full project description.

        Returns:
            dict: The structured dictionary output.
        """
        message = self.run_workflow(
            "project_structuring", {"project_description": project_description}
        )

        tree_data = restructure_for_tree(message)
        plot_project_tree(tree_data)
        return clean_json_string(message)

    def extract_sections(self, project_text):
        """
        Extracts all sections and subsections from the project text as a flat list.

        Args:
            project_text (str): The full project description.

        Returns:
            list: A list containing all sections and subsections as individual entries.
        """
        import re

        sections_list = []
        buffer = []
        section_pattern = r"^(\*{2}.+?\*{2}:)$"  # Match lines starting with '**' and ending with '**:'
        separator_pattern = r"^-{5,}$"  # Match lines with multiple dashes (separators)

        lines = project_text.splitlines()
        for line in lines:
            line = line.strip()

            if re.match(section_pattern, line):
                if buffer:
                    sections_list.append("\n".join(buffer).strip())
                    buffer = []
                sections_list.append(line.strip("**:"))
            elif re.match(separator_pattern, line):
                if buffer:
                    sections_list.append("\n".join(buffer).strip())
                    buffer = []
            else:
                buffer.append(line)

        if buffer:
            sections_list.append("\n".join(buffer).strip())

        return sections_list

    def build_departments(self, input_data: Dict[str, Any]) -> List[Department]:
        """
        Converts a structured project dictionary (as produced by
        setup_project_structure_nested_chat) into Department/Task objects.
        """
        departments = []

        for section_name, section_data in input_data["sections"].items():
            department = Department(section_name)

            if "assignments" in section_data:
                department.set_assignments(
                    teams=section_data["assignments"].get("teams", []),
                    individuals=section_data["assignments"].get("individuals", []),
                )

            for subsection_name, subsection_data in section_data.get("subsections", {}).items():
                department.add_subsection(subsection_name)

                for task_description in subsection_data.get("tasks", []):
                    task = Task(
                        title=task_description.split(":")[0],
                        description=task_description,
                        priority="High",  # Defaulting to High for simplicity
                    )
                    department.add_task(subsection_name, task)

                if "assignments" in subsection_data:
                    teams = subsection_data["assignments"].get("teams", [])
                    individuals = subsection_data["assignments"].get("individuals", [])
                    department.set_assignments(teams, individuals)

            departments.append(department)

        return departments

    # --- Event-driven supply-chain pipeline (unit U11 integration) --------
    #
    # Additive only: nothing above this point is touched. These two methods
    # are a thin bridge from the existing pyautogen-0.2-based CaptainAgent
    # onto the new event-driven, AutoGen-0.4-oriented supply-chain pipeline
    # assembled by agenten.orchestration.pipeline.build_pipeline() (units
    # U0-U5, U7-U10). The two subsystems are intentionally NOT merged into
    # one object graph -- CaptainAgent keeps its own `self.blockchain`/
    # `self.tools` for its existing methods untouched, and the pipeline
    # gets its own real Blockchain instance, exactly as build_pipeline()
    # does when called standalone (see examples/armada_demo.py).

    def build_default_supply_chain_pipeline(self, **pipeline_kwargs) -> "SupplyChainPipeline":
        """Thin wrapper around agenten.orchestration.pipeline.build_pipeline():
        builds the event-driven supply-chain pipeline and stores the result
        on self.supply_chain_pipeline.

        `llm_decompose` (required by build_pipeline) and, if wanted,
        `llm_judge` must be supplied via `pipeline_kwargs` -- this
        CaptainAgent's own `self.llm_config` is a pyautogen-0.2-style
        config dict, not directly usable as the plain async callables the
        new pipeline expects. Real LLM-backed implementations live in
        `agenten.llm` (make_llm_decompose/make_llm_judge); see
        examples/armada_demo.py for canned (non-LLM) examples.

        Ledger sharing: when the caller does not name a ledger explicitly
        (no `blockchain`, `storage`, or `blockchain_file_path` kwarg), the
        pipeline REUSES this Captain's own `self.blockchain` instance.
        This is deliberate: build_pipeline's standalone default file path
        ("blockchain.json") is the same file CaptainAgent's own default
        `blockchain_path` points at, and two independent Blockchain
        objects backed by the same file silently overwrite each other's
        blocks on every save (JSONFileStorage.save rewrites the whole
        file) -- sharing the single instance makes that collision
        impossible by default while keeping every explicit override
        honored verbatim.

        NOTE: the returned pipeline is NOT started. Callers must `await
        pipeline.start()` before submitting problems (see
        SupplyChainPipeline.start/submit_problem), and `await
        pipeline.stop()` when done.
        """
        from .orchestration.pipeline import build_pipeline

        if not any(k in pipeline_kwargs for k in ("blockchain", "storage", "blockchain_file_path")):
            pipeline_kwargs["blockchain"] = self.blockchain
        self.supply_chain_pipeline = build_pipeline(**pipeline_kwargs)
        return self.supply_chain_pipeline

    async def submit_problem_to_supply_chain(
        self, description: str, budget: Optional[DecompositionBudget] = None
    ) -> str:
        """Publish a ProblemSubmitted event onto the supply-chain pipeline's
        bus and return the generated problem_id.

        Requires build_default_supply_chain_pipeline() to have been called
        first (raises RuntimeError otherwise), AND the pipeline to have
        been started via `await self.supply_chain_pipeline.start()` --
        submit_problem itself raises RuntimeError on an unstarted pipeline,
        because publishing into one enqueues ledger writes that nothing
        ever drains (a problem_id would come back for work that silently
        never happens). Does not itself wait for the submitted problem to
        reach a terminal ledger stage -- callers poll
        `self.supply_chain_pipeline.wait_until_terminal(...)` /
        `self.supply_chain_pipeline.ledger_query` for that, same as
        examples/armada_demo.py and tests/test_e2e_smoke.py do.
        """
        pipeline = getattr(self, "supply_chain_pipeline", None)
        if pipeline is None:
            raise RuntimeError(
                "submit_problem_to_supply_chain: call build_default_supply_chain_pipeline() first"
            )
        return await pipeline.submit_problem(description, budget=budget)
