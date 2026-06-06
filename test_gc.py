import re
import sys
import requests
from bs4 import BeautifulSoup

BASE = "https://getcomics.org"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
_ISSUE_NUM_RE = re.compile(r'#(\d+(?:\.\d+)?)')

title = "Local Man"
issue_number = 25.0
store_date = "2024-01-01"

num_str = str(int(issue_number))
year = store_date[:4]

queries = [
    f"{title} #{num_str} ({year})",
    f"{title} #{num_str}",
    f"{title} {num_str}",
    title,
]

s = requests.Session()
s.headers.update(HEADERS)

for query in queries:
    print(f"\n→ searching: {query!r}")
    r = s.get(BASE, params={"s": query}, timeout=15)
    print(f"  status: {r.status_code}")
    if r.status_code == 429:
        print("  RATE LIMITED")
        continue

    soup = BeautifulSoup(r.text, "lxml")
    articles = soup.find_all("article", {"class": "post"})
    print(f"  articles found: {len(articles)}")

    for article in articles:
        h1 = article.find("h1", {"class": "post-title"})
        if not h1:
            continue
        a = h1.find("a")
        if not a:
            continue
        text = a.get_text(strip=True)
        href = a.get("href", "")
        nums = _ISSUE_NUM_RE.findall(text)
        matched = "local man" in text.lower() and any(float(n) == issue_number for n in nums)
        print(f"  {'✓' if matched else '·'} {text!r}  →  {href}")
        if matched:
            print(f"\n  FOUND: {href}")
            sys.exit(0)

print("\n  not found across all queries")
