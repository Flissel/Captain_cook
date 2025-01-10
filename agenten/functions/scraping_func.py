def scrape_search_results(query):
    """
    Scrapes Bing search results for the given query.

    Args:
        query (str): The search query.

    Returns:
        list: A list of dictionaries with title, link, and snippet for each search result.
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

    driver = webdriver.Chrome(service=Service(r'rap_project\new_struct\agenten\chromedriver-win64\chromedriver.exe'))
    
    driver.get(f"https://www.bing.com/search?q={query}")
    results = []
    try:
        elements = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "li.b_algo"))
        )
        for element in elements:
            title = element.find_element(By.TAG_NAME, "h2").text
            link = element.find_element(By.TAG_NAME, "a").get_attribute("href")
            try:
                snippet = element.find_element(By.CSS_SELECTOR, ".b_caption p").text
            except Exception:
                snippet = "Snippet not available"
            results.append({"title": title, "link": link, "snippet": snippet})
    except Exception as e:
        print(f"Error during scraping: {e}")
    finally:
        driver.quit()

    return results
