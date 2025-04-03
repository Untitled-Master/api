from bs4 import BeautifulSoup
import requests
import json
import os
import re
import concurrent.futures
import time
from functools import lru_cache

# Constants
ADMIN_URL = "https://web.animerco.org/wp-admin/admin-ajax.php"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
IMG_BB_API_KEY = "540a4171008b7d59dbc4cc88e8a8ce4b"
MAX_WORKERS = 10  # Limit concurrent requests to avoid rate limiting
LINKS_FILE = "all_anime_links.json"

# Create a session object to reuse for all requests
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


@lru_cache(maxsize=128)
def get_postid(url):
    """Extract the postid from a given episode page with caching"""
    try:
        response = session.get(url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        postid_input = soup.find("input", {"type": "hidden", "name": "postid"})

        return postid_input["value"] if postid_input else None
    except requests.exceptions.RequestException as e:
        print(f"Failed to retrieve page {url}, error: {e}")
        return None


def get_episode_embed(episode_url):
    """Get the embed URL for a specific episode"""
    postid_value = get_postid(episode_url)

    if not postid_value:
        print(f"Could not find postid for {episode_url}")
        return None

    headers = {
        "accept": "*/*",
        "accept-language": "en,en-GB;q=0.9,en-US;q=0.8",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "priority": "u=1, i",
        "sec-ch-ua": '"Chromium";v="134", "Not:A-Brand";v="24", "Microsoft Edge";v="134"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
        "Referer": episode_url,
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }

    payload = f"action=player_ajax&post={postid_value}&nume=1&type=tv"

    try:
        response = session.post(ADMIN_URL, headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"Failed to get embed for {episode_url}, error: {e}")
        return None


def extract_episode_number(url):
    """Extract episode number from URL using regex for better performance"""
    match = re.search(r'-(\d+)(?:-|$)', url)
    return int(match.group(1)) if match else None


def process_episode_link(link, index):
    """Process a single episode link and return its info"""
    # Try to extract episode number from URL or use the index
    ep_num = extract_episode_number(link) or index

    embed_info = get_episode_embed(link)

    if not embed_info:
        return None

    try:
        # Parse the JSON response
        embed_data = json.loads(embed_info)
        embed_url = embed_data.get("embed_url")

        if embed_url:
            result = {
                "episode": ep_num,
                "page_url": link,
                "embed_url": embed_url,
                "type": embed_data.get("type"),
            }
            print(f"Found embed URL for episode {ep_num}: {embed_url}")
            return str(ep_num), result
    except json.JSONDecodeError:
        print(f"Failed to parse JSON for episode {ep_num}")

    return None


def get_all_episodes(base_url):
    """Get all episode links and their embed URLs using parallel processing"""
    try:
        response = session.get(base_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        # Find all episode links - optimize selector for speed
        episodes_list = soup.select(".episodes-lists a[href]")

        # Remove duplicate URLs more efficiently
        unique_links = []
        seen_urls = set()

        for a_tag in episodes_list:
            link = a_tag["href"]
            if link not in seen_urls:
                unique_links.append(link)
                seen_urls.add(link)

        print(f"Found {len(episodes_list)} total links, {len(unique_links)} unique links for {base_url}")

        # Process links in parallel
        embed_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_link = {
                executor.submit(process_episode_link, link, i + 1): link
                for i, link in enumerate(unique_links)
            }

            for future in concurrent.futures.as_completed(future_to_link):
                link = future_to_link[future]
                try:
                    result = future.result()
                    if result:
                        ep_num, data = result
                        embed_results[ep_num] = data
                except Exception as e:
                    print(f"Error processing {link}: {e}")

        return embed_results
    except requests.exceptions.RequestException as e:
        print(f"Failed to retrieve main page {base_url}, error: {e}")
        return None


def upload_image_from_url(image_url):
    """Downloads an image from a URL and uploads it to ImgBB."""
    if not image_url:
        return None

    url = "https://api.imgbb.com/1/upload"
    payload = {"key": IMG_BB_API_KEY, "image": image_url}

    try:
        response = session.post(url, data=payload, timeout=15)
        response.raise_for_status()

        data = response.json()
        if data["success"]:
            return data["data"]["url"]
        else:
            print("ImgBB upload failed:", data)
            return None
    except Exception as e:
        print(f"ImgBB upload error: {e}")
        return None


def get_bg_image(base_url):
    """Get background image URL with improved selector"""
    try:
        response = session.get(base_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Directly find the element with a more specific selector
        a_tag = soup.select_one("div.anime-card.player a.image")

        if a_tag and a_tag.has_attr("data-src"):
            bg_image_url = a_tag["data-src"]
            return upload_image_from_url(bg_image_url)
        return None
    except requests.exceptions.RequestException as e:
        print(f"Failed to get background image for {base_url}, error: {e}")
        return None


def extract_info(base_url):
    """Extract anime info with improved error handling"""
    try:
        response = session.get(base_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        media_box = soup.find("div", class_="media-box")

        if not media_box:
            return {"error": "media-box not found"}

        # Extract genres more efficiently
        genre_list = []
        genres = media_box.find("div", class_="genres")
        if genres:
            genre_list = [{"text": a.text.strip()} for a in genres.find_all("a")]

        # Extract content
        content_div = media_box.find("div", class_="content")
        content_text = content_div.find("p").text.strip() if content_div and content_div.find("p") else None

        return {"genres": genre_list, "content": content_text}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {e}"}


def get_anime_name_from_url(base_url):
    """Extracts the anime name from the base URL with compiled regex for speed."""
    pattern = re.compile(r"https://web\.animerco\.org/seasons/([^/]+)/")
    match = pattern.search(base_url)
    return match.group(1) if match else "unknown_anime"


def sanitize_filename(filename):
    """Removes or replaces invalid characters from a filename with compiled regex."""
    # Replace spaces with underscores
    filename = filename.replace(" ", "_")
    # Remove any characters that are not alphanumeric, underscores, or hyphens
    return re.sub(r"[^\w\-]", "", filename)


def scrape_and_save(base_url):
    """
    Scrapes episode data from the given URL concurrently, including background image and
    anime info, and saves it to a JSON file named after the anime name.
    """
    start_time = time.time()

    # Validate URL
    if not base_url.startswith("https://web.animerco.org/"):
        print(f"Error: Invalid URL - {base_url}. Only URLs from web.animerco.org are supported")
        return

    # Create cache directory if it doesn't exist
    os.makedirs("cache", exist_ok=True)

    # Generate filename from URL
    anime_name = get_anime_name_from_url(base_url)
    safe_anime_name = sanitize_filename(anime_name)
    cache_file = f"cache/{safe_anime_name}.json"

    # Check if cache file exists and is recent (less than 1 day old)
    if os.path.exists(cache_file):
        file_age = time.time() - os.path.getmtime(cache_file)
        if file_age < 86400:  # 24 hours in seconds
            print(f"Using cached data from {cache_file} (age: {file_age/3600:.1f} hours)")
            return

    print(f"Scraping data for: {base_url}")

    # Run tasks concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        episodes_future = executor.submit(get_all_episodes, base_url)
        img_url_future = executor.submit(get_bg_image, base_url)
        info_future = executor.submit(extract_info, base_url)

        # Get results
        episodes = episodes_future.result()
        img_url = img_url_future.result()
        info = info_future.result()

    if episodes is None:
        print(f"Error: Failed to fetch episodes for {base_url}")
        return

    # Prepare data for saving
    data_to_save = {
        "success": True,
        "base_url": base_url,
        "imgUrl": img_url,
        "info": info,
        "episodes": episodes,
    }

    # Save to cache
    with open(cache_file, "w") as f:
        json.dump(data_to_save, f, indent=2)

    elapsed_time = time.time() - start_time
    print(f"Successfully scraped and saved data for {base_url} to {cache_file}")
    print(f"Process completed in {elapsed_time:.2f} seconds")


if __name__ == "__main__":
    if not os.path.exists(LINKS_FILE):
        print(f"Error: The file '{LINKS_FILE}' was not found.")
    else:
        try:
            with open(LINKS_FILE, "r") as f:
                data = json.load(f)
                anime_links = data.get("anime_links", [])

            if not anime_links:
                print(f"No anime links found in '{LINKS_FILE}'.")
            else:
                for base_url in anime_links:
                    scrape_and_save(base_url.strip())
                    time.sleep(1) # Be respectful to the server

        except json.JSONDecodeError:
            print(f"Error: Failed to decode JSON from '{LINKS_FILE}'. Please ensure it's a valid JSON file.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
