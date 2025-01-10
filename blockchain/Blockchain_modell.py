import plotly.graph_objects as go
import json
import hashlib
import os

class Block:
    def __init__(self, index, task, assigned_agents, status, previous_hash, parent_index=None, children=None, hash=None):
        self.index = index
        self.task = task
        self.assigned_agents = assigned_agents
        self.status = status
        self.previous_hash = previous_hash
        self.parent_index = parent_index
        self.children = children or []  # List of child block indices
        self.hash = hash or self.compute_hash()

    def compute_hash(self):
        block_string = f"{self.index}{self.task}{self.assigned_agents}{self.status}{self.previous_hash}{self.parent_index}{self.children}"
        return hashlib.sha256(block_string.encode()).hexdigest()

class Blockchain:
    def __init__(self, file_path="blockchain.json", reset_file=False):
        """
        Initialize the blockchain. Optionally reset the blockchain file.
        
        Args:
            file_path (str): Path to the blockchain file.
            reset_file (bool): If True, clears the file at the start.
        """
        self.file_path = file_path
        self.chain = []
        
        if reset_file:
            self.clear_file()  # Clear the file before initializing
        
        if os.path.exists(self.file_path) and not reset_file:
            self.load_from_file()
        else:
            self.create_genesis_block()

    def clear_file(self):
        """Clear the blockchain file by overwriting it with an empty JSON array."""
        with open(self.file_path, "w") as f:
            f.write("[]")  # Write an empty JSON array

    def create_genesis_block(self):
        genesis_block = Block(0, "Genesis Block", [], "completed", "0")
        self.chain.append(genesis_block)
        self.save_to_file()

    def add_block(self, task, assigned_agents, status="pending", parent_index=None):
        previous_hash = self.chain[-1].hash
        new_block = Block(len(self.chain), task, assigned_agents, status, previous_hash, parent_index=parent_index)
        
        # Update parent block with child reference
        if parent_index is not None:
            parent_block = self.chain[parent_index]
            parent_block.children.append(new_block.index)
            parent_block.hash = parent_block.compute_hash()  # Recompute parent hash

        self.chain.append(new_block)
        self.save_to_file()
        return new_block

    def save_to_file(self):
        temp_file_path = f"{self.file_path}.tmp"
        with open(temp_file_path, "w") as f:
            json.dump([block.__dict__ for block in self.chain], f, indent=4)
        os.replace(temp_file_path, self.file_path)

    def load_from_file(self):
        try:
            with open(self.file_path, "r") as f:
                data = json.load(f)
                self.chain = [Block(**block) for block in data]
        except (json.JSONDecodeError, FileNotFoundError):
            print(f"Blockchain file '{self.file_path}' is invalid or empty. Initializing with genesis block.")
            self.create_genesis_block()

    def get_block(self, index):
        return self.chain[index] if 0 <= index < len(self.chain) else None

    