import aiohttp
from bs4 import BeautifulSoup

async def extract_text_from_url(url):
    """
    Asynchronously extracts the text content from a webpage.

    Args:
        url (str): The URL of the webpage.

    Returns:
        str: Extracted text content.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, "html.parser")
                    paragraphs = soup.find_all("p")
                    return " ".join([p.get_text() for p in paragraphs])
                else:
                    print(f"Failed to fetch {url}: HTTP {response.status}")
                    return None
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return None
