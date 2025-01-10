import re ,json

def convert_to_json(raw_output: str) -> str:
     """
     Convert raw output into JSON format.

     Args:
         raw_output (str): Raw output string from the nested chat.

     Returns:
         str: JSON string of subtasks.
     """
     subtasks = []
     subtask_blocks = re.split(r"\*\*Subtask \d+: ", raw_output)[1:]  # Split on subtask headings

     for block in subtask_blocks:
         # Extract title
         title_match = re.match(r"(.*?)\n", block)
         title = title_match.group(1).strip() if title_match else "Untitled"

         # Extract description
         description_match = re.search(r"\*\*Description\*\*:\s*(.+?)(\n-|$)", block, re.DOTALL)
         description = description_match.group(1).strip() if description_match else ""

         # Extract priority
         priority_match = re.search(r"\*\*Priority\*\*:\s*(.+?)(\n-|$)", block)
         priority = priority_match.group(1).strip() if priority_match else "Medium"

         # Extract dependencies
         dependencies_match = re.search(r"\*\*Dependencies\*\*:\s*(.+?)(\n-|$)", block)
         dependencies = (
             [d.strip() for d in dependencies_match.group(1).split(",")]
             if dependencies_match and dependencies_match.group(1) != "None"
             else []
         )

         # Extract action steps
         action_steps_match = re.search(r"\*\*Action Steps\*\*:\s*(.+?)(\n\n|\Z)", block, re.DOTALL)
         action_steps = (
             [step.strip() for step in action_steps_match.group(1).split("\n") if step.strip()]
             if action_steps_match
             else []
         )

         # Create subtask dictionary
         subtask = {
             "title": title,
             "description": description,
             "priority": priority,
             "dependencies": dependencies,
             "action_steps": action_steps,
         }
         subtasks.append(subtask)

     return json.dumps({"subtasks": subtasks}, indent=2)

def clean_json(raw_json: str) -> str:
    """
    Clean and fix issues in the JSON string.

    Args:
        raw_json (str): The raw JSON string.

    Returns:
        str: Cleaned JSON string.
    """
    # Remove '**' from titles
    cleaned_json = raw_json.replace("**", "")

    # Return cleaned JSON string
    return cleaned_json