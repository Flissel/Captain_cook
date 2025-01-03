from agents.rap_text_team.idea_developer import IdeaDeveloper
from agents.rap_text_team.lyricist import Lyricist
from agents.rap_text_team.tone_specialist import ToneSpecialist
from agents.rap_text_team.flow_expert import FlowExpert
from agents.review_team.reviewer1 import Reviewer1
from agents.review_team.reviewer2 import Reviewer2
from agents.coordinator import CoordinatorAgent

def craft_rap_text(llm_config):
    # Initialize agents
    idea_developer = IdeaDeveloper(llm_config=llm_config)
    lyricist = Lyricist(llm_config=llm_config)
    tone_specialist = ToneSpecialist(llm_config=llm_config)
    flow_expert = FlowExpert(llm_config=llm_config)
    reviewer1 = Reviewer1(llm_config=llm_config)
    reviewer2 = Reviewer2(llm_config=llm_config)
    coordinator = CoordinatorAgent(llm_config=llm_config)
    
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
    
    # Fetch job descriptions
    job_descriptions = idea_developer.fetch_all_jobs(job_urls)
    if not job_descriptions:
        raise ValueError("No job descriptions could be fetched.")
    
    # Use the first job description for this example
    job_description = job_descriptions[0]["description"]
    
# Define the nested chat workflow
    nested_chats = [
    {
        "recipient": idea_developer,
        "message": "Extract themes and keywords from the job description.",
        "summary_method": "reflection_with_llm",
    },
    {
        "recipient": lyricist,
        "message": "Generate raw lyrics based on the extracted themes and keywords.",
        "summary_method": "reflection_with_llm",
    },
    {
        "recipient": tone_specialist,
        "message": "Adjust the tone of the lyrics to match the job's creative vision.",
        "summary_method": "reflection_with_llm",
    },
    {
        "recipient": flow_expert,
        "message": "Refine the flow of the lyrics for better rhythm and delivery.",
        "summary_method": "reflection_with_llm",
    },
    {
        "recipient": reviewer1,
        "message": "Review the lyrics for structure and coherence.",
        "summary_method": "last_msg",
    },
    {
        "recipient": reviewer2,
        "message": "Provide additional feedback on the creative elements of the lyrics.",
        "summary_method": "last_msg",
    },
    {
        "recipient": coordinator,
        "message": "Integrate feedback and finalize the rap text.",
        "summary_method": "reflection_with_llm",
    },
    ]
    coordinator.register_nested_chats(
    nested_chats,
    trigger=lambda sender: sender not in [
        idea_developer,
        lyricist,
        tone_specialist,
        flow_expert,
        reviewer1,
        reviewer2,
        coordinator,
    ],
    )
    print("Nested chat workflow registered successfully.")
    # Trigger the nested chat by sending a message to the Coordinator
    reply = coordinator.generate_reply(
        messages=[
            {
                "role": "user",
                "content": (
                    "Craft a rap text based on this job description:\n\n"
                    "At Suno, we are building a future where anyone can make music. "
                    "You can make a song for any moment with just a few short words..."
                ),
            }
        ]
    )
    
    print("Final Output:", reply)

# Example usage
if __name__ == "__main__":
    from config.llm_config import API_KEY, MODEL
    llm_config = {
        "config_list": [{"model": MODEL, "api_key": API_KEY}]
    }
    job_description = """
    Your job description or fetched job description here.
    """
    final_rap_text = craft_rap_text(llm_config)
    print("Final Rap Text:\n", final_rap_text)
