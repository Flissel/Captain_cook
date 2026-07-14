from agenten.Captain import CaptainAgent
from config.llm_config import API_KEY, MODEL
from agenten.subtaskGenerator import SubtaskGenerator
from agenten.project_definer import execute_project_definition


def main():
    # Define LLM configuration
    llm_config = {
        "config_list": [{"model": MODEL, "api_key": API_KEY}]
    }

    # Initialize CaptainAgent
    captain = CaptainAgent(name="CaptainAgent", llm_config=llm_config)

    # Project Description
    project_description = """
    Develop a Multi-agent-system which can craft and execute a whole Project in context of a given project description.
    There is no limit to the number of agents, but the number of agents should be as small as possible.
    """

    # Refine the project description
    project_description = execute_project_definition(project_description, captain)

    # Split into departments/sections and record the project as the root blockchain block
    project_split, sections = captain.automate_project_split(project_description)
    departments = captain.build_departments(project_split)
    # TODO: assign teams/individuals to departments once a "team_assignment"
    # workflow is registered (see docs/ARCHITECTURE.md for how to add one).
    project_block = captain.add_task_to_blockchain(
        task=project_description,
        assigned_agents=[],
        status="in_progress",
    )

    # Generate a system prompt for executing the project, and decompose it into subtasks
    system_prompt = captain.make_system_prompt(project_description)
    subtask_generator = SubtaskGenerator(system_prompt=system_prompt)
    subtasks = subtask_generator.generate_prompts(project_description, captain)

    # Record each subtask as a child block of the project block
    for subtask in subtasks:
        captain.add_task_to_blockchain(
            task=subtask["title"],
            assigned_agents=["Agent1", "Agent2"],  # Replace with dynamic agent assignment
            status="pending",
            parent_index=project_block.index,
        )


if __name__ == "__main__":
    main()
