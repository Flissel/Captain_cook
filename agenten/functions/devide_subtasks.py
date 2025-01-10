import re


def divide_output_into_subtasks(raw_output):
        """
        Divide the project output into its individual subtasks.

        Args:
            raw_output (str): The raw output string containing subtasks.

        Returns:
            list: A list of dictionaries, each representing a subtask.
        """
        # Regex pattern to identify subtasks
        subtask_pattern = r"### Subtask (\d+): (.+?)\n- \*\*Description:\*\* (.+?)\n- \*\*Priority:\*\* (.+?)\n- \*\*Dependencies:\*\* (.+?)\n-+"

        subtasks = []
        matches = re.finditer(subtask_pattern, raw_output, re.DOTALL)

        for match in matches:
            subtask_number = int(match.group(1))
            title = match.group(2).strip()
            description = match.group(3).strip()
            priority = match.group(4).strip()
            dependencies = match.group(5).strip()
            dependencies = dependencies.split(", ") if dependencies != "None" else []

            subtasks.append({
                "subtask_number": subtask_number,
                "title": title,
                "description": description,
                "priority": priority,
                "dependencies": dependencies
            })

        return subtasks