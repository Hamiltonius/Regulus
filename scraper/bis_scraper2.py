from requests_html import HTMLSession
import pandas as pd
from datetime import datetime
import os

def fetch_bis_news():
    session = HTMLSession()
    url = "https://www.bis.doc.gov/index.php/all-articles/17-about-bis/newsroom"
    r = session.get(url)
    r.html.render(timeout=20, args=["--no-sandbox", "--disable-setuid-sandbox"])

    articles = r.html.find("div.catItemView")
    data = []
    for article in articles:
        print("📦 Found article block")

        title_tag = article.find("h3.catItemTitle", first=True)
        date_tag = article.find("dd.published", first=True)

        print("🔍 Title tag:", title_tag)
        print("📅 Date tag:", date_tag)

        if not title_tag:
            print("⚠️ Skipping article: no title tag found.")
            continue

        a_tag = title_tag.find("a", first=True)
        title = title_tag.text.strip() if title_tag else "N/A"
        link = "https://www.bis.doc.gov" + a_tag.attrs["href"] if a_tag else "N/A"
        date_text = date_tag.text.strip() if date_tag else "N/A"

        print(f"✅ Parsed:\n  Title: {title}\n  Link: {link}\n  Date: {date_text}\n")

        try:
            date = datetime.strptime(date_text, "%B %d, %Y")
        except (ValueError, TypeError):
            date = None

        data.append({"title": title, "url": link, "date": date})

    df = pd.DataFrame(data)

    if "date" in df.columns and not df["date"].isnull().all():
        df.sort_values(by="date", ascending=False, inplace=True)
    else:
        print("⚠️  'date' column missing or empty. Skipping sort.")

    today = datetime.today().strftime('%Y-%m-%d')
    os.makedirs("data/raw", exist_ok=True)
    df.to_csv(f"data/raw/bis_news_{today}.csv", index=False)

if __name__ == "__main__":
    df = fetch_bis_news()
    print("\nTop 5 BIS News Items:\n")
    print(df.head())

