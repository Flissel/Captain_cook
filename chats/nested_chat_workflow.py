import os
from utils.logger import setup_logger
from agents.user_proxy_agent import UserAgent
from agents.coordinator import CoordinatorAgent
from agents.rap_text_team.idea_developer import IdeaDeveloper
from agents.rap_text_team.lyricist import Lyricist
from agents.rap_text_team.flow_expert import FlowExpert
from agents.rap_text_team.tone_specialist import ToneSpecialist
from agents.review_team.reviewer1 import Reviewer1
from agents.review_team.reviewer2 import Reviewer2

# Initialize logger
logger = setup_logger("NestedChatWorkflow")


def nested_chat_workflow(llm_config):
    """Executes the nested chat workflow with dynamic role allocation."""
    try:
        # Initialize agents
        user = UserAgent(name="User")
        coordinator = CoordinatorAgent(name="Coordinator", llm_config=llm_config)
        lyricist = Lyricist(name="Lyricist", llm_config=llm_config)
        flow_expert = FlowExpert(name="FlowExpert", llm_config=llm_config)
        tone_specialist = ToneSpecialist(name="ToneSpecialist", llm_config=llm_config)
        idea_developer = IdeaDeveloper(name="IdeaDeveloper", llm_config=llm_config)
        reviewer1 = Reviewer1(name="Reviewer1", llm_config=llm_config)
        reviewer2 = Reviewer2(name="Reviewer2", llm_config=llm_config)

        # Job URLs
        job_urls = [
            "https://jobs.ashbyhq.com/suno/3464381a-52b6-44dc-8403-0d16125319fd",
            "https://jobs.ashbyhq.com/suno/bbed444d-2a9f-4f81-a7f0-c9c21c033f1a",
            "https://jobs.ashbyhq.com/suno/1e23d125-d72c-49b6-891d-77d62c96cd13",
            "https://jobs.ashbyhq.com/suno/d5fbf9f8-85c5-42cf-b5e1-cb197a1608e1",
            "https://jobs.ashbyhq.com/suno/6237a7a4-2b5c-4f24-8616-1e2c437d4c2e",
            "https://jobs.ashbyhq.com/suno/adb5c5ef-5897-4acb-8014-0a7f162742d8",
            "https://jobs.ashbyhq.com/suno/9e6da9b6-8562-4d9e-ae8e-c3319f76bdba",
            "https://jobs.ashbyhq.com/suno/23bb487a-2fce-4732-acfe-9ec760c97b2f",
            "https://jobs.ashbyhq.com/suno/c87801d1-4b65-4f1f-9ede-821e7f84ea4c",
            "https://jobs.ashbyhq.com/suno/161291ab-ccdd-443f-98fb-2184e8572543",
            "https://jobs.ashbyhq.com/suno/909d0a2b-0915-44ea-82ab-ca8e049e2412",
            "https://jobs.ashbyhq.com/suno/7522d696-7ce8-4ece-a983-4be03dffde20"
        ]

        # Step 1: Fetch job descriptions
        logger.info("Fetching job descriptions...")
        job_descriptions = idea_developer.fetch_all_jobs(job_urls)
        if not job_descriptions:
            raise ValueError("Failed to fetch job descriptions.")
        logger.info(f"Fetched {len(job_descriptions)} job descriptions.")

        # Process each job
        for job in job_descriptions:
            logger.info(f"Processing job: {job['url']}")
            
            # Step 2: Rap Text Team Sub-chat
            logger.info("Starting Rap Text Team sub-chat...")
            rap_text_team = [lyricist, flow_expert, tone_specialist]
            rap_team_chat = idea_developer.initiate_chat(
                recipient = rap_text_team,
                message=f"Create rap lyrics about this job: {job}"
                )
            logger.info("Rap Text Team sub-chat completed.")

            # Step 3: Review Team Sub-chat
            logger.info("Starting Review Team sub-chat...")
            review_team = [reviewer1, reviewer2]
            review_team_chat = reviewer1.initiate_chat(
                recipient = lyricist,
                assistant_agents=review_team,
                message="Review the rap text and provide feedback.",
                parent_chat=rap_team_chat  # Linking chats
            )
            logger.info("Review Team sub-chat completed.")

            # Step 4: Finalization with Coordinator
            logger.info("Starting Coordinator Finalization chat...")
            final_chat = coordinator.initiate_chat(
                assistant_agents=[coordinator],
                message="Integrate feedback and finalize the rap text.",
                parent_chat=review_team_chat  # Linking chats
            )
            logger.info("Coordinator finalization completed.")

            # Save chat logs
            save_chat_logs(final_chat)
            logger.info("Chat logs saved.")

    except Exception as e:
        logger.error(f"An error occurred during the workflow: {e}")
        raise

def save_chat_logs(chat):
    """Saves chat logs to a file for review."""
    try:
        logs_dir = "chat_logs"
        os.makedirs(logs_dir, exist_ok=True)
        log_file = os.path.join(logs_dir, "final_chat_log.txt")

        with open(log_file, "w") as f:
            for message in chat.get_messages():
                f.write(f"{message['sender']}: {message['content']}\n")

        logger.info(f"Chat logs saved to {log_file}")
    except Exception as e:
        logger.error(f"Failed to save chat logs: {e}")
        raise