from agenten.Captain import CaptainAgent  # Replace with the actual script name
from config.llm_config import API_KEY, MODEL
from agenten.subtaskGenerator import SubtaskGenerator
from agenten.project_definer import execute_project_definition

def main():
    # Define LLM configuration
    llm_config = {
        "config_list": [{"model": MODEL, "api_key": API_KEY}]  # Replace with your OpenAI API key
    }

    # Initialize CaptainAgent
    captain = CaptainAgent(name="CaptainAgent", llm_config=llm_config )

    # Project Description
    project_description = """
    Develop a Multi-agent-system which can craft and execute a whole Project in context of a given project description. 
    There is no limit to the number of agents, but the number of agents should be as small as possible.
    """

    # Execute the workflow
    project_description = execute_project_definition(project_description, captain)
    
    project_split, sections = captain.automate_project_split(project_description)
    departments = captain.build_departments(project_split)

    #TODO Fill departements with life cycles
    #TODO assign teams to departments
        # -Generate and validate tasks 
    #TODO assign individuals to teams
    #TODO    
    # Generate and validate subtasks
    task_description = project_description

    system_prompt = captain.make_system_prompt(project_description)
    
    captain_initial_agent = CaptainAgent(name="CaptainAgent", llm_config=llm_config , system_message=system_prompt)
    
    #TODO: spilt into Tasks
    #TODO: assginment to agents team 
    #TODO: assginment to agents individual
    #TODO: assginment to Department 
    #TODO: assginment to Task
    #TODO. assginment of a Context Team 
    


    subtask_generator = SubtaskGenerator(system_prompt=project_description)
    subtasks = subtask_generator.extract_subtasks(task_description, captain)
    
   # analyse_subtasks = subtask_generator.extract_subtasks(subtasks, captain)
   # print(analyse_subtasks)
   # analyse_subtasks = subtask_generator.extract_subtasks(captain)
#
    # Add subtasks to the blockchain as child blocks
    for subtask in subtasks:
        captain.add_task_to_blockchain(
            task=subtask["title"],
            assigned_agents=["Agent1", "Agent2"],  # Replace with dynamic agent assignment
            status="pending",
            parent_index=project_block.index
        )

    # Display hierarchical blockchain
    #print("\n--- Blockchain Hierarchy ---")
    
    #captain.blockchain.generate_interactive_graph()


if __name__ == "__main__":
    main()
