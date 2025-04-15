import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import os


def fetch_bis_news():
    url = "https://www.bis.doc.gov/index.php/all-articles/17-about-bis/newsroom"
    response = requests.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    
    data = []
    print("\n\n==== PAGE DEBUG START ====\n")
    print(soup.prettify()[0:3000])  # Print the first 3,000 characters
    print("\n==== PAGE DEBUG END ====\n")

    # Look for all articles in <div class="items-row"> containers
    articles = soup.find_all("div", class_="catItemView")

    for article in articles:
        a_tag = article.find("a")
        title = a_tag.text.strip() if a_tag else "N/A"
        link = "https://www.bis.doc.gov" + a_tag["href"] if a_tag else "N/A"

        # Find <dd class="published"> inside the article block
        date_tag = article.find("dd", class_="published")
        date_text = date_tag.text.strip() if date_tag else "N/A"

        print(f"Title: {title}\nLink: {link}\nDate: {date_text}\n")

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

    return df
