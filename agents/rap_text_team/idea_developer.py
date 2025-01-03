from autogen import ConversableAgent
import requests
from bs4 import BeautifulSoup
import json
import pandas as pd

class IdeaDeveloper(ConversableAgent):
    def __init__(self, name="IdeaDeveloper", llm_config=None):
        super().__init__(
            name=name,
            silent=True,
            llm_config=llm_config,
            human_input_mode="NEVER",  # Fully autonomous
            is_termination_msg=lambda msg: "terminate" in msg["content"].lower()
        )
        print(f"{self.name} initialized as a Conversable Idea Developer Agent.")

    def fetch_job_description_from_link(self, job_url):
        """Fetches the job description from a specific job link."""
        try:
            response = requests.get(job_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # Extract JSON-LD script containing job description
            script_tag = soup.find('script', type="application/ld+json")
            if script_tag:
                job_data = json.loads(script_tag.string)
                description_html = job_data.get("description", "No description available.")
                description_text = BeautifulSoup(description_html, 'html.parser').get_text(strip=True)
                return description_text
            else:
                raise ValueError(f"Job description not found on page: {job_url}")
        except requests.exceptions.RequestException as e:
            print(f"Error fetching job description from {job_url}: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON data from {job_url}: {e}")
            return None

    def fetch_all_jobs(self, job_urls):
        """Fetches job descriptions for all job links provided."""
        jobs = []
        for job_url in job_urls:
            print(f"Fetching job description from: {job_url}")
            job_description = self.fetch_job_description_from_link(job_url)
            if job_description:
                jobs.append({"url": job_url, "description": job_description})
        return jobs

    def process_dataframe(self, dataframe):
        """Processes the provided DataFrame to extract information."""
        processed_data = []
        for _, row in dataframe.iterrows():
            feature = row["Feature"]
            purpose = row["Purpose"]
            implementation = row["Implementation"]
            benefits = row["Benefits"]
            detailed_implementation = row["Detailed Implementation"]
            department = row["Department"]
            processed_data.append({
                "Feature": feature,
                "Purpose": purpose,
                "Implementation": implementation,
                "Benefits": benefits,
                "Detailed Implementation": detailed_implementation,
                "Department": department,
            })
        return processed_data
    

    


