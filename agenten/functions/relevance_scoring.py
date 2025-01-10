def score_relevance(query, content):
    """
    Scores the relevance of the content to the query using semantic similarity.

    Args:
        query (str): The search query.
        content (str): Extracted content from the webpage.

    Returns:
        float: A score representing the relevance (higher is better).
    """
    from sentence_transformers import SentenceTransformer, util

    model = SentenceTransformer("all-MiniLM-L6-v2")  # Example model
    query_embedding = model.encode(query, convert_to_tensor=True)
    content_embedding = model.encode(content, convert_to_tensor=True)
    return util.cos_sim(query_embedding, content_embedding).item()