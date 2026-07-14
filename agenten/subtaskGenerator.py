from typing import List, Optional
from pydantic import BaseModel, Field
import json
from .functions.devide_subtasks import divide_output_into_subtasks
from .workflows.registry import get_workflow


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
        Extract main tasks and their subtasks from the system prompt via the
        'subtask_extraction' workflow.

        Args:
            captain: The CaptainAgent instance managing the nested chats.

        Returns:
            str: Text output of the subtasks after nested chat processing.
        """
        return get_workflow("subtask_extraction").run(
            captain, context={"system_prompt": self.system_prompt}
        )

    def generate_prompts(self, task_description: str, captain) -> list:
        """
        Generate subtasks from the given task description via the
        'subtask_generation' workflow, then parse and persist them.

        Args:
            task_description (str): Description of the primary task.
            captain: The CaptainAgent instance managing the nested chats.

        Returns:
            list: Parsed subtasks (each a dict with title/description/priority/dependencies).
        """
        subtasks_text = get_workflow("subtask_generation").run(
            captain, context={"task_description": task_description}
        )
        subtasks = divide_output_into_subtasks(subtasks_text)

        with open("subtasks.json", "w") as file:
            json.dump(subtasks, file, indent=4)

        return subtasks
