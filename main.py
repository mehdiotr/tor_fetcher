import asyncio
import json
import random
import re
from datetime import datetime
from playwright.async_api import async_playwright, Error as PlaywrightError
import aiohttp
from aiohttp_socks import ProxyConnector
from stem import Signal
from stem.control import Controller
from colorama import Fore, Style, init as colorama_init

TARGET_URL = "https://www.google.com/"
MAX_RETRIES = 30
REQUEST_TIMEOUT = 60000
TOR_SOCKS_PROXY = "socks5://127.0.0.1:9050"
TOR_CONTROL_PORT = 9051
TOR_CONTROL_PASSWORD = None

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/109.0.5414.74",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Mobile/15E148 Safari/604.1"
]

DEFAULT_VIEWPORT = {'width': 1280, 'height': 720}

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def _renew_tor_connection_sync(control_port, control_password):
    try:
        with Controller.from_port(port=control_port) as controller:
            if control_password:
                controller.authenticate(password=control_password)
            else:
                controller.authenticate()
            controller.signal(Signal.NEWNYM)
            print(Fore.GREEN + "SUCCESS: NEWNYM signal sent to Tor.")
            return True
    except Exception as e:
        print(Fore.RED + f"ERROR: Could not connect to Tor control port ({control_port}) or send NEWNYM signal: {e}")
        print(Fore.RED + "Ensure Tor is running and ControlPort is configured correctly in your torrc file.")
        print(Fore.RED + "If using HashedControlPassword, provide the original password.")
        return False

async def renew_tor_connection(control_port=TOR_CONTROL_PORT, control_password=TOR_CONTROL_PASSWORD):
    loop = asyncio.get_running_loop()
    print(Fore.LIGHTBLACK_EX + "Attempting to renew Tor IP address...")
    success = await loop.run_in_executor(None, _renew_tor_connection_sync, control_port, control_password)
    if success:
        print(Fore.LIGHTBLACK_EX + "Waiting 10 seconds for Tor to establish a new circuit...")
        await asyncio.sleep(10)
    return success

async def check_tor_connectivity_aiohttp(proxy_url=TOR_SOCKS_PROXY, test_url="https://check.torproject.org/api/ip"):
    print(Fore.LIGHTBLACK_EX + f"INFO: Checking Tor connectivity using aiohttp via proxy {proxy_url} to {test_url}...")
    try:
        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(test_url, timeout=20) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("IsTor"):
                        print(Fore.GREEN + f"SUCCESS: aiohttp check: Successfully connected via Tor. Current IP: {data.get('IP')}")
                        return True
                    else:
                        print(Fore.RED + f"WARNING: aiohttp check: Connected, but not identified as Tor. IP: {data.get('IP')}")
                        return False
                else:
                    print(Fore.RED + f"ERROR: aiohttp check: Failed to connect. Status: {response.status}")
                    return False
    except Exception as e:
        print(Fore.RED + f"ERROR: aiohttp check: Error during Tor connectivity test: {e}")
        print(Fore.RED + "Ensure 'aiohttp_socks' is installed: pip install aiohttp_socks")
        return False

async def fetch_page_with_tor_playwright(url: str, output_html_file: str):
    final_page_content = None
    final_captured_headers = None
    overall_success = False
    browser = None

    async with async_playwright() as p:
        for attempt in range(MAX_RETRIES):
            print(Fore.LIGHTBLACK_EX + f"\n--- Attempt {attempt + 1}/{MAX_RETRIES} to fetch {url} ---")
            current_user_agent = get_random_user_agent()
            print(Fore.LIGHTBLACK_EX + f"Using User-Agent: {current_user_agent}")

            context = None
            try:
                if not browser or not browser.is_connected():
                    if browser:
                        await browser.close()
                    print(Fore.LIGHTBLACK_EX + "Launching new browser instance with Tor proxy...")
                    browser = await p.chromium.launch(
                        headless=True,
                        proxy={"server": TOR_SOCKS_PROXY}
                    )

                context = await browser.new_context(
                    user_agent=current_user_agent,
                    viewport=DEFAULT_VIEWPORT
                )
                page = await context.new_page()

                print(Fore.LIGHTBLACK_EX + f"Navigating to {url}...")
                response_object = await page.goto(url, timeout=REQUEST_TIMEOUT, wait_until="networkidle")

                if response_object and response_object.ok:
                    temp_page_content = await page.content()
                    temp_captured_headers = await response_object.all_headers()

                    content_valid = temp_page_content and \
                                    ("</html>" in temp_page_content.lower() or \
                                     "</body>" in temp_page_content.lower())
                    headers_valid = bool(temp_captured_headers)

                    if content_valid and headers_valid:
                        print(Fore.GREEN + f"SUCCESS: Fetched and validated page content from {response_object.url} (Status: {response_object.status}).")
                        final_page_content = temp_page_content
                        final_captured_headers = temp_captured_headers
                        overall_success = True
                    elif content_valid and not headers_valid:
                        print(Fore.RED + f"WARNING: Page content OK from {response_object.url} (Status: {response_object.status}), but failed to retrieve headers. Retrying.")
                    elif not content_valid and headers_valid:
                         print(Fore.RED + f"WARNING: Headers retrieved for {response_object.url} (Status: {response_object.status}), but page content validation failed. Retrying.")
                    else:
                        print(Fore.RED + f"WARNING: Page content validation failed AND headers not retrieved from {response_object.url} (Status: {response_object.status}). Retrying.")
                else:
                    status = response_object.status if response_object else "No response object"
                    print(Fore.RED + f"ERROR: Failed to fetch page. Status: {status}")

                await context.close()
                context = None

                if overall_success:
                    break

            except PlaywrightError as e:
                print(Fore.RED + f"ERROR: Playwright error during attempt {attempt + 1}: {e}")
                if "net::ERR_PROXY_CONNECTION_FAILED" in str(e) or \
                   "Target page, context or browser has been closed" in str(e) or \
                   "Browser closed" in str(e).lower():
                    print(Fore.RED + "Proxy connection failed or browser issue detected. Will try to renew Tor IP and restart browser.")
                    if browser and browser.is_connected():
                        await browser.close()
                    browser = None
            except Exception as e:
                print(Fore.RED + f"ERROR: General error during attempt {attempt + 1}: {e}")
                if browser and browser.is_connected():
                    await browser.close()
                browser = None
            finally:
                if context:
                    await context.close()

            if overall_success:
                break
            if attempt < MAX_RETRIES - 1:
                await renew_tor_connection()
            else:
                print(Fore.LIGHTBLACK_EX + "INFO: Max retries reached.")

        if browser and browser.is_connected():
            await browser.close()

    if overall_success and final_page_content and final_captured_headers:
        print(Fore.GREEN + "\n--- Captured Headers ---")
        try:
            print(Fore.GREEN + json.dumps(final_captured_headers, indent=4))
        except Exception as e:
            print(Fore.RED + f"Error printing headers: {e}")
            print(Fore.RED + str(final_captured_headers))


        try:
            with open(output_html_file, "w", encoding="utf-8") as f:
                f.write(final_page_content)
            print(Fore.GREEN + f"SUCCESS: Page content saved to {output_html_file}")
            return True
        except IOError as e:
            print(Fore.RED + f"ERROR: Could not write HTML output file {output_html_file}: {e}")
            return False
    else:
        print(Fore.RED + f"FAILURE: Failed to fetch complete data for {url} after {MAX_RETRIES} attempts.")
        if not final_page_content: print(Fore.RED + "Reason: Page content was empty or not retrieved/validated.")
        if not final_captured_headers: print(Fore.RED + "Reason: Main document headers were not captured.")
        return False

async def main():
    colorama_init(autoreset=True)
    print(Fore.LIGHTBLACK_EX + "--- Starting Web Scraper ---")
    print(Fore.LIGHTBLACK_EX + f"Target URL: {TARGET_URL}")
    print(Fore.LIGHTBLACK_EX + f"Max Retries: {MAX_RETRIES}")
    print(Fore.LIGHTBLACK_EX + f"Tor SOCKS Proxy: {TOR_SOCKS_PROXY}")
    print(Fore.LIGHTBLACK_EX + f"Tor Control Port: {TOR_CONTROL_PORT}")

    tor_ready = await check_tor_connectivity_aiohttp()
    if not tor_ready:
        print(Fore.RED + "WARNING: Initial Tor check with aiohttp failed or indicated not using Tor.")
        print(Fore.RED + "Proceeding with Playwright, but Tor might not be configured correctly for aiohttp check or at all.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cleaned_url_for_filename = re.sub(r'^https?://', '', TARGET_URL)
    cleaned_url_for_filename = re.sub(r'[^\w.-]', '_', cleaned_url_for_filename)
    cleaned_url_for_filename = cleaned_url_for_filename.strip('_.-')
    dynamic_html_filename = f"{cleaned_url_for_filename}_{timestamp}.html"
    
    print(Fore.LIGHTBLACK_EX + f"\nOutput HTML file will be: {dynamic_html_filename}")
    print(Fore.LIGHTBLACK_EX + "\n--- Starting Playwright Fetching Process ---")
    fetch_successful = await fetch_page_with_tor_playwright(TARGET_URL, dynamic_html_filename)

    if fetch_successful:
        print(Fore.GREEN + "\n--- Process Completed Successfully ---")
    else:
        print(Fore.RED + "\n--- Process Failed ---")

if __name__ == "__main__":
    asyncio.run(main())
