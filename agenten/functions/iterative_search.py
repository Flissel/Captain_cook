from extract_content_from_url import extract_text_from_url
from scraping_func import scrape_search_results
from relevance_scoring import score_relevance
def iterative_search(query, relevance_threshold=0.8):
    """
    Performs iterative search and extracts the most relevant content.

    Args:
        query (str): The search query.
        relevance_threshold (float): The minimum relevance score to accept.

    Returns:
        dict: Most relevant content with its score, URL, and raw content.
    """
    results = scrape_search_results(query)
    if not results:
        print("No results found.")
        return None

    scored_results = []
    for result in results:
        print(f"Processing: {result['link']}")
        content = extract_text_from_url(result["link"])
        if content:
            score = score_relevance(query, content)
            scored_results.append({
                "title": result["title"],
                "link": result["link"],
                "snippet": result["snippet"],
                "score": score,
                "content": content,
            })

    # Sort results by score
    scored_results = sorted(scored_results, key=lambda x: x["score"], reverse=True)

    # Return the most relevant result above the threshold
    for result in scored_results:
        if result["score"] >= relevance_threshold:
            print(f"Highly relevant content found: {result['link']}")
            return result

    print("No highly relevant content found.")
    return scored_results[0] if scored_results else None
