from typing import List, Dict, Optional

class Task:
    def __init__(self, title: str, description: str, priority: str = "Medium", dependencies: Optional[List[str]] = None):
        self.title = title
        self.description = description
        self.priority = priority
        self.dependencies = dependencies or []

    def __repr__(self):
        return f"Task(title={self.title}, priority={self.priority}, dependencies={self.dependencies})"


class Department:
    def __init__(self, name: str):
        self.name = name
        self.subsections: Dict[str, Dict[str, List[Task]]] = {}
        self.assignments: Dict[str, List[str]] = {"teams": [], "individuals": []}

    def add_subsection(self, subsection_name: str):
        if subsection_name not in self.subsections:
            self.subsections[subsection_name] = {"tasks": []}

    def add_task(self, subsection_name: str, task: Task):
        if subsection_name not in self.subsections:
            self.add_subsection(subsection_name)
        self.subsections[subsection_name]["tasks"].append(task)

    def set_assignments(self, teams: List[str], individuals: List[str]):
        self.assignments["teams"] = teams
        self.assignments["individuals"] = individuals

    def get_tasks(self, subsection_name: Optional[str] = None) -> List[Task]:
        if subsection_name:
            return self.subsections.get(subsection_name, {}).get("tasks", [])
        all_tasks = []
        for subsection in self.subsections.values():
            all_tasks.extend(subsection["tasks"])
        return all_tasks

    def summary(self):
        return {
            "name": self.name,
            "subsections": {name: len(data["tasks"]) for name, data in self.subsections.items()},
            "assignments": self.assignments,
        }

    def __repr__(self):
        return f"Department(name={self.name}, subsections={list(self.subsections.keys())})"
