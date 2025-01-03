import pandas as pd
import re
import json
class JobIdeaMerger:
    def __init__(self):
        self.merged_data = []

    def merge_single_job(self, job_description, ideas_with_scores):
        """
        Merges a single job description with its matching ideas.

        Args:
            job_description (str): Description of the job.
            ideas_with_scores (list): List of matching ideas with scores.
                Example: [{"idea": "idea1", "score": 95}, ...]

        Returns:
            None
        """
        
        # Add each idea for the given job
        for i, idea_data in enumerate(ideas_with_scores, start=1):
            self.merged_data.append({
                "ID": i,
                "url": job_description["url"],
                "Job Description": job_description['description'],
                "Idea": idea_data["idea"],
                "Score": idea_data["score"]
            })

    def get_merged_data(self):
        """
        Returns the merged data as a DataFrame.

        Returns:
            pd.DataFrame: Merged job descriptions and matching ideas.
        """
        return pd.DataFrame(self.merged_data)
    


    def parse_ideas_to_format(self,ideas_string):
        """
        Parses a string of ideas with scores into a list of dictionaries.

        Args:
            ideas_string (str): A string containing ideas and their scores in the format:
                '- {"1": "Interactive Widgets", "score": 90}\n...'

        Returns:
            list: A list of dictionaries with keys 'idea' and 'score'.
                Example: [{"idea": "Interactive Widgets", "score": 90}, ...]
        """
        # Regular expression to match JSON-like entries in the string
        idea_pattern = re.compile(r'-\s*({.*?})')
        matches = idea_pattern.findall(ideas_string)

        parsed_ideas = []
        for match in matches:
            try:
                # Parse the JSON-like string into a dictionary
                idea_dict = json.loads(match)
                # Convert the dictionary into the required format
                idea_id = next(iter(idea_dict))  # Get the first key (e.g., "1")
                parsed_ideas.append({
                    "idea": idea_dict[idea_id],  # Use the value of the first key as the idea
                    "score": idea_dict["score"]
                })
            except json.JSONDecodeError:
                print(f"Failed to parse idea: {match}")

        return parsed_ideas

    def write_to_excel(self, file_path):
        """
        Writes the merged data to an Excel file.

        Args:
            file_path (str): The path to the Excel file to write.
        """
        try:
            # Convert the merged data to a DataFrame
            df = self.get_merged_data()
            
            # Write the DataFrame to an Excel file
            df.to_excel(file_path, index=False, engine='openpyxl')
            print(f"Merged data successfully written to {file_path}")
        except Exception as e:
            print(f"An error occurred while writing to Excel: {e}")
# Example usage

