from autogen import ConversableAgent, UserProxyAgent , AssistantAgent
from .Internet_searcher import InternetSearcher
from blockchain.Blockchain_modell import Blockchain
from blockchain.visualisation import create_project_knowledge_graph ,restructure_for_tree,plot_project_tree
from typing import List, Dict, Any, Optional
from .functions.Caiptain_functions.task_models import Task, Department
from .functions.Caiptain_functions.clean_json_string import clean_json_string

class CaptainAgent:
    def __init__(self, name, llm_config, system_message= "None for yet", blockchain_path="blockchain.json"):
        self.name = name
        self.llm_config = llm_config
        self.system_message=system_message
        self.agents = {}
        self.blockchain = Blockchain(blockchain_path)

    def add_task_to_blockchain(self, task, assigned_agents=[], status="pending", parent_index=None):
        block = self.blockchain.add_block(task, assigned_agents, status, parent_index)
        return block

    def update_task_status_in_blockchain(self, index, status):
        self.blockchain.update_task_status(index, status)

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
            code_execution_config = False,
            is_termination_msg=lambda msg: "terminate" in msg["content"].lower() or "approve" in msg["content"].lower(),
        )
        self.agents[agent_name] = agent
        return agent

    def create_internet_searcher(self, tool_name="internet_searcher"):
        """
        Creates and registers an InternetSearcher as a tool for the Captain Agent.

        Args:
            tool_name (str): The name of the tool under which the InternetSearcher is registered.

        Returns:
            callable: The search function of the InternetSearcher.
        """
        async def internet_search(query):
            searcher = InternetSearcher()
            results = await searcher.search_and_score(query)
            return results

        # Register the search function
        if tool_name not in self.agents:
            self.agents[tool_name] = internet_search
        return internet_search

    def make_system_prompt(self, task_description):
        """
        Create a system prompt using nested chat logic.
        """
        # Step 1: Create agents
        generator_agent = self.create_agent_assistant("GeneratorAgent", "You create system prompts.")
        critic_agent = self.create_agent_assistant("CriticAgent", "You critique the generated system prompts.")
        user_proxy_agent = self.create_agent_user_proxy("UserProxyAgent", "You are the user proxy and orchestrate the task.")

        # Step 2: Define reflection and update messages
        def reflection_message(recipient, messages, sender, config):
            if not sender.chat_messages_for_summary(recipient):
                return "No content to reflect on."
            return f"Reflect on: {sender.chat_messages_for_summary(recipient)[-1]['content']}"

        def update_message(recipient, messages, sender, config):
            if not sender.chat_messages_for_summary(recipient):
                return "Provide updates based on prior discussions."
            return f"Update based on critique: {sender.chat_messages_for_summary(recipient)[-1]['content']}"

        # Step 3: Define the nested chat queue
        nested_chat_queue = [
            {
                "recipient": critic_agent,
                "message": reflection_message,
                "summary_method": "reflection_with_llm",
                "max_turns": 1,
            },
            {
                "recipient": generator_agent,
                "message": update_message,
                "summary_method": "last_msg",
                "max_turns": 1,
            },
            {
                "recipient": critic_agent,
                "message": reflection_message,
                "summary_method": "reflection_with_llm",
                "max_turns": 1,
            },
            {
                "recipient": generator_agent,
                "message": update_message,
                "summary_method": "last_msg",
                "max_turns": 1,
            },
        ]

        # Step 4: Register the nested chats
        generator_agent.register_nested_chats(
            chat_queue=nested_chat_queue,
            trigger=user_proxy_agent,
        )

        # Step 5: Initiate the chat
        trigger_message = user_proxy_agent.initiate_chat(
            recipient=generator_agent,
            message={"content": f"Create a system prompt for the task: {task_description}."},
            max_turns=1,
        )

        # Step 6: Extract the final output
        if not trigger_message.chat_history:
            raise RuntimeError("Chat history is empty. The nested chat may not have triggered correctly.")
        return trigger_message.chat_history[-1]["content"]
        
        #TODO: spilt into Tasks
        #TODO: assginment to agents team 
        #TODO: assginment to agents individual
        #TODO: assginment to Department 
        #TODO: assginment to Task
        #TODO. assginment of a Context Team 
    def automate_project_split(self, project_text):
        """
        Automates the process of splitting a project into hierarchical components and subtasks.

        Args:
            project_text (str): The full project description.

        Returns:
            dict: A structured representation of the project with sections, subtasks, and assignments.
        """
        # Step 1: Extract sections
        sections = self.extract_sections(project_text)
        Project_tree_dict = self.setup_project_structure_nested_chat(project_text)
        return Project_tree_dict ,sections



    def setup_project_structure_nested_chat(self,project_description):
        """
        Sets up a nested chat workflow to process a structured project description into a detailed hierarchical dictionary.

        Args:
            project_description (str): The full project description.
            captain (CaptainAgent): The Captain agent orchestrating the nested chat workflow.

        Returns:
            dict: The structured dictionary output.
        """
        # Step 1: Create agents
        generator_agent = self.create_agent_assistant(
            "GeneratorAgent",
            "You are responsible for breaking down the input into a structured dictionary with sections, subsections, and tasks. "
            "Ensure that all information is logically divided and actionable. Follow this format:\n"
            "{\n"
            "  'overview': ['List of all major sections'],\n"
            "  'sections': {\n"
            "    'Section Title': {\n"
            "      'subsections': {\n"
            "        'Subsection Title': {\n"
            "          'tasks': ['Task 1', 'Task 2'],\n"
            "          'assignments': {\n"
            "            'teams': ['Team 1'],\n"
            "            'individuals': ['Individual 1']\n"
            "          }\n"
            "        }\n"
            "      },\n"
            "      'assignments': {\n"
            "        'teams': ['Team 1'],\n"
            "        'individuals': ['Individual 1']\n"
            "      }\n"
            "    }\n"
            "  },\n"
            "  'tasks': ['Flat list of all tasks'],\n"
            "  'context_teams': ['List of collaborating teams']\n"
            "}\n"
        )

        critic_agent = self.create_agent_assistant(
            "CriticAgent",
            "You review and refine the structured dictionary generated by GeneratorAgent. "
            "Focus on completeness, clarity, and logical organization. Ensure tasks and assignments are actionable and logically grouped. "
            "Provide constructive feedback and suggest improvements where necessary."
        )

        structured_output_agent = self.create_agent_assistant(
            "StructuredOutputAgent",
            "You finalize the structured dictionary into a clean, hierarchical format. "
            "Ensure it is formatted as per the requested structure and contains all information clearly organized."
        )

        user_proxy_agent = self.create_agent_user_proxy(
            "UserProxyAgent",
            "You manage the workflow between GeneratorAgent, CriticAgent, and StructuredOutputAgent. "
            "Ensure smooth transitions and proper task execution, and provide the final structured output."
        )

        # Step 2: Define the nested chat workflow
        nested_chats = [
            {
                "recipient": generator_agent,
                "message": (
                    f"Analyze the project description: {project_description}. "
                    "Break it down into a structured dictionary with sections, subsections, tasks, and assignments."
                ),
                "summary_method": "last_msg",
                "max_turns": 2,
                "carryover": f"ALLWAYS INCLUDE the PROJECT DESCRIPTION:\n {project_description}\n IN THE SORT OUT WHAT NOT IS IN THE CONTEXT OF PROGRAMMING IDEAS GATHERING AND DESGINING PROZESSES ARE GOOD FOR ORGANZIE TASKS{project_description}",
            },
            {
                "recipient": critic_agent,
                "message": (
                    "Review the structured dictionary generated by GeneratorAgent. Ensure it is complete, actionable, "
                    "and logically organized. Provide feedback and suggest refinements if necessary."
                ),
                "summary_method": "reflection_with_llm",
                "carryover": f"ALLWAYS INCLUDE the PROJECT DESCRIPTION:\n {project_description}\n IN THE SORT OUT WHAT NOT IS IN THE CONTEXT OF PROGRAMMING IDEAS GATHERING AND DESGINING PROZESSES ARE GOOD FOR ORGANZIE TASKS{project_description}",
                "max_turns": 1,
                
            },
            {
                "recipient": generator_agent,
                "message": (
                    "Refine the structured dictionary based on feedback from CriticAgent. "
                    "Ensure all suggestions are incorporated. Respond ONLY with the refined dictionary."
                ),
                "summary_method": "last_msg",
                "carryover": f"ALLWAYS INCLUDE the PROJECT DESCRIPTION:\n {project_description}\n IN THE SORT OUT WHAT NOT IS IN THE CONTEXT OF PROGRAMMING IDEAS GATHERING AND DESGINING PROZESSES ARE GOOD FOR ORGANZIE TASKS{project_description}",
                "max_turns": 1,
            },
            {
                "recipient": structured_output_agent,
                "message": (
                    "Finalize the refined structured dictionary into a clean, hierarchical format. "
                    "Ensure it adheres to the requested structure with sections, subsections, tasks, and assignments. "
                    "Respond ONLY with the final structured dictionary."
                ),
                "carryover": f"ALLWAYS INCLUDE the PROJECT DESCRIPTION:\n {project_description}\n IN THE SORT OUT WHAT NOT IS IN THE CONTEXT OF PROGRAMMING IDEAS GATHERING AND DESGINING PROZESSES ARE GOOD FOR ORGANZIE TASKS{project_description}",
                "summary_method": "last_msg",
                "max_turns": 1,
            },
        ]

        # Step 3: Register the workflow
        generator_agent.register_nested_chats(chat_queue=nested_chats, trigger=user_proxy_agent)

        # Step 4: Initiate the workflow
        trigger_message = user_proxy_agent.initiate_chat(
            recipient=generator_agent,
            message={"content": "Start processing the project description into a structured dictionary."},
            max_turns=len(nested_chats),
        )

        # Step 5: Extract the final structured output
        if not trigger_message.chat_history:
            raise RuntimeError("Nested chat did not produce a result.")
        message = trigger_message.chat_history[-1]["content"]

        tree_data = restructure_for_tree(message)
        plot_project_tree(tree_data)
        final_output = clean_json_string(message)
       # create_project_knowledge_graph(final_output)

        return final_output


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
    
        # Split the text into lines
        lines = project_text.splitlines()
        for line in lines:
            line = line.strip()
    
            # Check for section or subsection titles
            if re.match(section_pattern, line):
                # Add the previous buffer content if present
                if buffer:
                    sections_list.append("\n".join(buffer).strip())
                    buffer = []
                sections_list.append(line.strip("**:"))  # Add the section title
            elif re.match(separator_pattern, line):
                # Separator detected; save the buffer and reset
                if buffer:
                    sections_list.append("\n".join(buffer).strip())
                    buffer = []
            else:
                # Collect content for the current section
                buffer.append(line)
    
        # Add any remaining buffer content
        if buffer:
            sections_list.append("\n".join(buffer).strip())
    
        return sections_list

    def process_section_list(raw_list):
        """
        Processes a raw list of sections into a clean, structured list.

        Args:
            raw_list (list): The raw list of sections.

        Returns:
            list: A structured list with each section as a separate entry.
        """
        processed_list = []
        for item in raw_list:
            # Split sections and subsections cleanly
            split_items = item.split("\n\n")  # Split by double newlines to separate major sections
            for sub_item in split_items:
                cleaned_item = sub_item.strip()
                if cleaned_item:
                    processed_list.append(cleaned_item)
        return processed_list


    from typing import List, Dict, Any, Optional

# Reuse the previously defined Task and Department classes

# Parsing input data
    def build_departments(self,input_data: Dict[str, Any]) -> List[Department]:
        departments = []

        # Iterate through the sections in the input
        for section_name, section_data in input_data["sections"].items():
            # Create a Department for each section
            department = Department(section_name)

            # Assign department-level teams and individuals
            if "assignments" in section_data:
                department.set_assignments(
                    teams=section_data["assignments"].get("teams", []),
                    individuals=section_data["assignments"].get("individuals", []),
                )

            # Process subsections and tasks
            for subsection_name, subsection_data in section_data.get("subsections", {}).items():
                department.add_subsection(subsection_name)

                # Add tasks to each subsection
                for task_description in subsection_data.get("tasks", []):
                    task = Task(
                        title=task_description.split(":")[0],
                        description=task_description,
                        priority="High"  # Defaulting to High for simplicity
                    )
                    department.add_task(subsection_name, task)

                # Assign subsection-level teams and individuals
                if "assignments" in subsection_data:
                    teams = subsection_data["assignments"].get("teams", [])
                    individuals = subsection_data["assignments"].get("individuals", [])
                    department.set_assignments(teams, individuals)

            departments.append(department)

        return departments





