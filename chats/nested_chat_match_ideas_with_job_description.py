from autogen import ConversableAgent
from agents.rap_text_team.idea_developer import IdeaDeveloper
from agents.rap_text_team.context_provider import ContextProvider
from agents.coordinator import CoordinatorAgent
def setup_nested_chat(llm_config, job_description, ideas):
    """
    Sets up the nested chat workflow between the IdeaDeveloper and ContextProvider agents.

    Args:
        llm_config (dict): Configuration for the language model.
        job_descriptions (list): List of job descriptions.
        ideas (str): Existing ideas to refine.

    Returns:
        ConversableAgent: The IdeaDeveloper agent with nested chat registered.
    """
    # Initialize agents with LLM configurations
    idea_developer_agent = IdeaDeveloper(name="IdeaDeveloper", llm_config=llm_config)
    context_provider = ContextProvider(name="ContextProvider", llm_config=llm_config)

    # Iterate through job descriptions and setup nested chats
    nested_chats = [
            {
                "recipient": context_provider,
                "message": """Extract the main requirements and key themes from the job description.
                              Match the requirements with the features in the provided ideas.
                              Respond with an overview that lists each job requirement and the matching ideas/features for each requirement.""",
                "summary_method": "last_msg",
                "carryover": f"{job_description}" + f"{ideas}",
                "context": f"",
                "max_turns": 1,
            },
            {
                "recipient": idea_developer_agent,
                "message": """Critique each idea in the list based on how well it matches the job requirements.
                              Assign a score from 0 to 100 for the match, with 100 being a perfect match.
                              Provide a critique for each idea explaining why it matches well or poorly.
                              Order the output by the highest score first.
                              Add a blank line to separate the critique and scores.
                              Then, respond with the Job Description and the ideas with their respective scores.""",
                "summary_method": "last_msg",
                "context": f"{ideas}",
                "carryover": f"{job_description}" + f"{ideas}",
                "max_turns": 1,
            },
            {
                "recipient": context_provider,
                "message": """Based on the critique and scores provided:
                              - Respond with an overview of all ideas that scored above 70%.
                              - Format the output for easy reading, grouping the ideas by their scores in descending order.
                              - Clearly indicate which features are considered "good" for the job (above 70%).
                              Respond with the following format:
                              - {"1": "Interactive Widgets", "score": 95},
                                {"2": "Spotify Integration", "score": 90} 
                              - Append "Terminate" at the end of the response.""",
                "summary_method": "reflection_with_llm",
                "max_turns": 1,
            },
        ]


        # Register nested chats
    idea_developer_agent.register_nested_chats(
        nested_chats,
        max_turns=1,
        trigger=lambda sender: sender not in [idea_developer_agent, context_provider],
        
    )

    return idea_developer_agent

def analyze_job_description( job_description, ideas, llm_config):
    """
    Triggers the nested chat and analyzes the received themes and keywords.

    Args:
        idea_developer (ConversableAgent): The initialized IdeaDeveloper agent.
        job_description (str): The job description to analyze.

    Returns:
        dict: The analyzed output containing themes and refined context.
    """
    idea_developer_agent = setup_nested_chat(llm_config=llm_config,ideas=ideas, job_description=job_description)
    coordinator_agent = CoordinatorAgent(name="Coordinator", llm_config=llm_config)
    
    reply = coordinator_agent.initiate_chat(

        recipient=idea_developer_agent,
        messages=[
            {
                "content": f"Match the ideas to the job description: \f{job_description} \f{ideas} to provide a refined context."
            }
        ]
        
    ) 


    return reply.chat_history[1]["content"]
