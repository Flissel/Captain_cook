from config.llm_config import API_KEY, MODEL
from chats.nested_chat_workflow import nested_chat_workflow
from chats.nested_chat_crafting import craft_rap_text
import pandas as pd
from chats.nested_chat_match_ideas_with_job_description import setup_nested_chat, analyze_job_description
from chats.helpers import JobIdeaMerger
from agents.rap_text_team.idea_developer import IdeaDeveloper
def main():
    llm_config = {
        "config_list": [{"model": MODEL, "api_key": API_KEY}]
    }

    # Initialize IdeaDeveloper
    idea_developer = IdeaDeveloper(llm_config=llm_config)
    # merge CLass
    job_merger = JobIdeaMerger()

    # Example job URLs
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
   
    # Example DataFrame processing
    file_path = "rap_project/ideas/Platform_Features_with_Departments.csv"
    try:
        df = pd.read_csv(file_path)
        processed_data = idea_developer.process_dataframe(df)

    except FileNotFoundError as e:
        print(f"Error reading file: {e}")
        return

    # Step 2: Analyze the first job description using nested chat
    if job_descriptions:
        for job_description in job_descriptions:

            ideas_with_scores = analyze_job_description(job_description, processed_data, llm_config)
            formated_ideas = job_merger.parse_ideas_to_format(ideas_with_scores) 
            job_merger.merge_single_job(job_description, formated_ideas)       
                   
    else:
        print("No job descriptions available for analysis.")

    df = job_merger.get_merged_data()    
    job_merger.write_to_excel("rap_project/ideas/data_for_rap_text.xlsx")
    # Step 3: Craft RAP text using nested chat


if __name__ == "__main__":
    main()





   # craft_rap_text(llm_config)



    #nested_chat_workflow(llm_config)