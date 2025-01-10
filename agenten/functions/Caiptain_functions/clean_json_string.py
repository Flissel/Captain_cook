import re
import json

def clean_json_string(raw_json_string):
    """
    Cleans up a raw JSON string by removing unwanted characters like
    leading/trailing quotes, escape sequences, and extra whitespace.

    Args:
        raw_json_string (str): The raw JSON string to clean.

    Returns:
        dict: Parsed and cleaned JSON as a dictionary.
    """
    # Remove leading/trailing quotes and unnecessary characters
    cleaned_string = raw_json_string.strip()  # Remove leading/trailing whitespace
    cleaned_string = re.sub(r"^['\"`]+|['\"`]+$", "", cleaned_string)  # Remove quotes (`'`, `"`, ```)

    # Replace escape sequences for proper JSON formatting
    cleaned_string = cleaned_string.replace("\n", "").replace("\t", "").replace("\r", "").replace("json", "")

    # Convert the cleaned string into a dictionary
    try:
        cleaned_json = json.loads(cleaned_string)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON: {e}")

    return cleaned_json