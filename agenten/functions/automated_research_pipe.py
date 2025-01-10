def automated_research_pipeline(query):
    """
    Automates the internet research pipeline using scraping, content extraction,
    and relevance scoring.

    Args:
        query (str): The search query.

    Returns:
        dict: The most relevant content and metadata.
    """
    from iterative_search import iterative_search
    
    # Perform iterative search
    result = iterative_search(query, relevance_threshold=0.85)
    
    if result:
        print(f"Most Relevant Result:\nTitle: {result['title']}\nLink: {result['link']}\n")
        return result
    else:
        print("No relevant content found.")
        return None

"""# Example Usage
query = "Making musik with ai github repos"
automated_research_pipeline(query)
from extract_content_from_url import extract_text_from_url

url = "https://www.restack.io/p/ai-in-music-composition-answer-top-github-repositories-cat-ai"
content = extract_text_from_url(url)
print(content)
from relevance_scoring import score_relevance

query = "AI in music composition GitHub repositories"
relevance_score = score_relevance(query, content)
print(f"Relevance Score: {relevance_score}")"""
