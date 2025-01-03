"""User Proxy Agent."""

from autogen import UserProxyAgent

class UserAgent(UserProxyAgent):
    def __init__(self, name="User"):
        super().__init__(name, code_execution_config=False)
        print(f"{self.name} initialized as the User Proxy Agent.")
