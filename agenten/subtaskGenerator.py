from typing import List, Dict, Optional
from pydantic import BaseModel, Field
import json
from .functions.devide_subtasks import divide_output_into_subtasks
from .functions.convert_subtasks import convert_to_json, clean_json 
from .functions.make_graph import generate_interactive_subtask_graph
class Subtask(BaseModel):
    title: str = Field(..., description="Title of the subtask")
    description: str = Field(..., description="Detailed description of the subtask")
    priority: str = Field(..., description="Priority of the subtask (High, Medium, Low)")
    dependencies: Optional[List[str]] = Field(
        default=[], description="List of dependent subtasks, if any"
    )


class TaskSchema(BaseModel):
    subtasks: List[Subtask] = Field(..., description="List of all subtasks")

class SubtaskGenerator:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt

    def extract_subtasks(self, captain) -> str:
        """
        Extract main tasks and their subtasks from the system prompt.

        Args:
            captain: The CaptainAgent instance managing the nested chats.

        Returns:
            str: JSON string of the subtasks after nested chat processing.
        """
        # Create agents
        generator_agent = captain.create_agent_assistant(
            "GeneratorAgent", 
            "You create subtasks based on the provided Tasks."
        )
        critic_agent = captain.create_agent_assistant(
            "CriticAgent", 
            "You review and provide feedback on the generated subtasks."
        )
        user_proxy_agent = captain.create_agent_user_proxy(
            "UserProxyAgent", 
            "You orchestrate the task and manage nested chat flows."
        )

        # Define dynamic reflection and update messages
        def reflection_message(recipient, messages, sender, config):
            print(f"Reflection triggered by {sender.name} for {recipient.name}")
            if not sender.chat_messages_for_summary(recipient):
                print("No previous messages found for reflection.")
                return "No content to reflect on."
            return f"Reflect on the following and provide critique: {sender.chat_messages_for_summary(recipient)[-1]['content']}"

        def update_work_message(recipient, messages, sender, config):
            print(f"Update triggered by {sender.name} for {recipient.name}")
            if not sender.chat_messages_for_summary(recipient):
                print("No previous messages found for updates.")
                return "Provide updates without critique."
            return f"Refine your work based on the critique: {sender.chat_messages_for_summary(recipient)[-1]['content']}"

        # Define the nested chat workflow
        nested_chat_queue = [
            {
                "recipient": generator_agent,
                "message": f"Decompose the following task into smaller subtasks: {self.system_prompt}. "
                           "Ensure each subtask includes a title, description, priority (High, Medium, Low), "
                           "and dependencies, if applicable.",
                "summary_method": "last_msg",
                "max_turns": 1,
            },
            {
                "recipient": critic_agent,
                "message": reflection_message,
                "summary_method": "last_msg",
                "max_turns": 1,
            },
            {
                "recipient": generator_agent,
                "message": update_work_message,
                "summary_method": "reflection_with_llm",
                "max_turns": 1,
            },
        ]

        # Register nested chats
        generator_agent.register_nested_chats(
            nested_chat_queue,
            trigger=user_proxy_agent,
        )

        # Start the conversation
        print("Starting nested chat to generate subtasks...")
        trigger_message = user_proxy_agent.initiate_chat(
            recipient=generator_agent,
            message={
                "content": f"Please decompose the following system prompt into smaller subtasks: {self.system_prompt}."
            },
            max_turns=1,
        )

        # Extract the final subtasks
        if not trigger_message.chat_history:
            raise RuntimeError("Chat history is empty. Nested chat may not have triggered correctly.")

        final_output = trigger_message.chat_history[-2]["content"]
        print("\n--- Final Subtasks ---")
        print(final_output)

        return final_output


    def generate_prompts(self, task_description: str, captain) -> TaskSchema:
        """
        Generate subtasks from the given task description using a nested chat workflow.

        Args:
            task_description (str): Description of the primary task.
            captain: The CaptainAgent instance managing the nested chats.

        Returns:
            TaskSchema: Validated subtasks as a TaskSchema object.
        """
        # Create agents
        # Create agents
        generator_agent = captain.create_agent_assistant("GeneratorAgent", "You generate a perfect system prompt to solve the given Task.")
        critic_agent = captain.create_agent_assistant("CriticAgent", "You critique the generated system prompt if it's solving the Task.")
        user_proxy_agent = captain.create_agent_user_proxy("UserProxyAgent", "You are the user proxy and orchestrate the task.")

        # Define the nested chat workflow
        nested_chat_queue = [
            {
                "recipient": generator_agent,
                "message": f"Decompose the following task into structured subtasks: {task_description}. "
                           f"Ensure each subtask includes title, description, priority (High, Medium, Low), and dependencies.",
                "summary_method": "last_msg",
                "max_turns": 1,
            },
            {
                "recipient": critic_agent,
                "message": """Please review the generated subtasks.
                              - Ensure the subtasks are clear, actionable, and well-structured.
                              - it must be in struct that llm can handle best.""",
                "summary_method": "last_msg",
                "max_turns": 1,
            },
            {
                "recipient": generator_agent,
                "message": """Refine the subtasks based on feedback from CriticAgent.
                              Ensure the subtasks are clear, actionable, and well-structured.
                              devide the SUBTASKS with '---------------------------------------'
                              Ensure all fields are filled correctly and dependencies are accurate.""",
                "summary_method": "last_msg",
                "max_turns": 1,
            },
        ]

        # Register nested chats
        generator_agent.register_nested_chats(
            chat_queue=nested_chat_queue,
            trigger=user_proxy_agent,
        )

        # Start the conversation
        trigger_message = user_proxy_agent.initiate_chat(
            recipient=generator_agent,
            message={"content": f"Decompose the following task into structured subtasks: {task_description}."},
            max_turns=1,
        )

        # Extract and validate the subtasks using Pydantic
        subtasks_json = trigger_message.chat_history[-1]["content"]
        subtasks = divide_output_into_subtasks(subtasks_json)
        

        # Save subtasks to a JSON file
        with open("subtasks.json", "w") as file:
            json.dump(subtasks, file, indent=4)

        print("Subtasks saved to subtasks.json!")
       # generate_interactive_subtask_graph(subtasks)
        return subtasks


