#!/usr/bin/env python3
"""
Norwegian Cruise Line price-watcher
 – URL      : see TARGET_URL below
 – Checks   : base fare (PP / USD)  +  taxes, fees & port expenses
 – Interval : every N minutes (default 180 = 3 h)
 – Alerts   : Telegram bot message if price OR tax changes
Environment variables required:
  TELEGRAM_BOT_TOKEN   – token of your Telegram bot
  TELEGRAM_CHAT_ID     – chat/channel/user id to receive alerts
  CHECK_INTERVAL_MIN   – optional; minutes between checks (int)
  LAST_PRICE_FILE      – optional; path to store last-seen figures
"""

import os, re, time, json, sys, logging
import requests
from bs4 import BeautifulSoup

TARGET_URL = ("https://www.ncl.com/no/en/cruises/"
              "14-day-iceland-round-trip-london-reykjavik-belfast-"
              "and-paris-PRIMA14SOULEHBFSREYISAAKUGNRBGOSVGSOU"
              "?destinations=4294949354,4294949395,4294949385"
              "&sailMonths=4294949333&numberOfGuests=4294949461"
              "&sortBy=price&autoPopulate=f&from=resultpage"
              "&itineraryCode=PRIMA14SOULEHBFSREYISAAKUGNRBGOSVGSOU")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36")
}

# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    """
    Fetch HTML content with session reuse and retry logic for better reliability
    """
    session = requests.Session()
    # Add headers to mimic a real browser
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })

    # Add retry adapter
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        return r.text
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to fetch HTML: {e}")


def parse_price(html: str):
    """
    Returns (base_price, taxes) as floats.
    Uses multiple parsing strategies for robustness:
    1. JSON-LD structured data (most reliable)
    2. HTML element parsing with BeautifulSoup
    3. Regex fallback
    """

    # Strategy 1: Parse JSON-LD structured data (most reliable)
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Find the JSON-LD script tag
        json_scripts = soup.find_all('script', type='application/ld+json')
        for script in json_scripts:
            try:
                data = json.loads(script.string)
                if data.get('@type') == 'Offer' and 'price' in data:
                    base_price = float(data['price'])

                    # Now find taxes in the HTML
                    # Look for the specific div structure mentioned
                    disclaimer_div = soup.find('div', class_='c544_disclaimer')
                    if disclaimer_div:
                        disclaimer_list = disclaimer_div.find('ul', class_='c544_disclaimer_list')
                        if disclaimer_list:
                            for item in disclaimer_list.find_all('li', class_='c544_disclaimer_list_item'):
                                span = item.find('span')
                                if span and span.text:
                                    # Extract tax amount from text like "+ Taxes, fees and port expenses $493.55 USD"
                                    tax_match = re.search(r'\$([0-9,]+\.?[0-9]*)', span.text)
                                    if tax_match:
                                        taxes = float(tax_match.group(1).replace(',', ''))
                                        return base_price, taxes

                    # If we found base price but not taxes, continue to other strategies
                    break
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except Exception:
        pass

    # Strategy 2: HTML element parsing with BeautifulSoup
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Look for price in various possible locations
        base_price = None
        taxes = None

        # Try to find price in common price display elements
        price_elements = soup.find_all(['span', 'div', 'p'], class_=re.compile(r'price|cost|fare'))
        for elem in price_elements:
            if elem.text:
                # Look for pattern like "$1,899" followed by "PP / USD"
                price_match = re.search(r'\$([0-9,]+)\s*PP\s*/\s*USD', elem.text, re.I)
                if price_match:
                    base_price = float(price_match.group(1).replace(',', ''))
                    break

        # Look for taxes in disclaimer section
        disclaimer_div = soup.find('div', class_='c544_disclaimer')
        if disclaimer_div:
            disclaimer_text = disclaimer_div.get_text()
            tax_match = re.search(r'Taxes,\s*fees\s*and\s*port\s*expenses\s*\$([0-9,]+\.?[0-9]*)', disclaimer_text,
                                  re.I)
            if tax_match:
                taxes = float(tax_match.group(1).replace(',', ''))

        if base_price is not None and taxes is not None:
            return base_price, taxes

    except Exception:
        pass

    # Strategy 3: Regex fallback (original approach, but improved)
    try:
        # Clean up the HTML text
        text = re.sub(r'\s+', ' ', html)

        # Look for base fare - try multiple patterns
        base_price = None
        fare_patterns = [
            r'\$([0-9,]+)\s*PP\s*/\s*USD',  # Original pattern
            r'"price":\s*"([0-9,]+)"',  # JSON price field
            r'priceFrom["\']?\s*:\s*["\']?([0-9,]+)',  # Price from field
        ]

        for pattern in fare_patterns:
            m_fare = re.search(pattern, text, re.I)
            if m_fare:
                base_price = float(m_fare.group(1).replace(',', ''))
                break

        # Look for taxes - try multiple patterns
        taxes = None
        tax_patterns = [
            r'Taxes,\s*fees\s*and\s*port\s*expenses\s*\$([0-9,]+\.?[0-9]*)',  # Original pattern
            r'\+\s*Taxes,\s*fees\s*and\s*port\s*expenses\s*\$([0-9,]+\.?[0-9]*)',  # With + prefix
            r'taxesAndFees["\']?\s*:\s*["\']?([0-9,]+\.?[0-9]*)',  # JSON tax field
        ]

        for pattern in tax_patterns:
            m_tax = re.search(pattern, text, re.I)
            if m_tax:
                taxes = float(m_tax.group(1).replace(',', ''))
                break

        if base_price is not None and taxes is not None:
            return base_price, taxes

    except Exception:
        pass

    # If all strategies fail, raise an error
    raise ValueError("Could not locate price block in HTML using any parsing strategy")


def load_last(path):
    """Load last saved price data from JSON file"""
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load last price data: {e}")
            return None
    return None


def save_last(path, data):
    """Save current price data to JSON file"""
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        print(f"Warning: Could not save price data: {e}")


def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": message})
    r.raise_for_status()

# -----------------------------------------------------------------------------
# Main monitoring loop
# -----------------------------------------------------------------------------
def run():
    # configuration
    bot_token = "7915288038:AAGzGuF0aMgaJoYYU-EzsA2tdIxRavQkjKc"
    chat_id   = "-1002852664251"
    interval  = int(os.getenv("CHECK_INTERVAL_MIN", "1"))
    store_f   = os.getenv("LAST_PRICE_FILE", "last_price.json")
    if not (bot_token and chat_id):
        sys.exit("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s — %(levelname)s — %(message)s"
    )

    while True:
        try:
            html = fetch_html(TARGET_URL)
            base, taxes = parse_price(html)

            debug_msg = (
                f"[DEBUG] Price check → Base: ${base:,.0f}, "
                f"Taxes: ${taxes:,.2f} (Total: ${base + taxes:,.2f})"
            )
            logging.info(debug_msg)

            # compare to last-seen
            last = load_last(store_f)
            if last is None or (base, taxes) != tuple(last):
                # send alert
                msg = (f"Norwegian Prima price update🚢 \n\n"
                       f"Base fare: ${base:,.0f}\n"
                       f"Taxes/fees: ${taxes:,.2f}\n"
                       f"Total: ${base + taxes:,.2f}\n\n"
                       f"{TARGET_URL}")
                send_telegram(bot_token, chat_id, msg)
                save_last(store_f, [base, taxes])

        except Exception as e:
            logging.error(f"Problem during check: {e}")

        time.sleep(interval * 60)

if __name__ == "__main__":
    run()
