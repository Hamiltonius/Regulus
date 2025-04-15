from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
import os
import time
import re
import requests

def setup_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    service = Service("/opt/homebrew/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def download_pdf(url, folder="data/pdfs"):
    if not url.endswith(".pdf"):
        return None
    
    os.makedirs(folder, exist_ok=True)
    filename = url.split("/")[-1]
    if not filename:
        return None
        
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            path = os.path.join(folder, filename)
            with open(path, "wb") as f:
                f.write(response.content)
            print(f"📄 PDF saved: {filename}")
            return path
        else:
            print(f"⚠️ Failed to download {url}: Status code {response.status_code}")
            return None
    except Exception as e:
        print(f"⚠️ Failed to download {url}: {e}")
        return None

def parse_date(date_text):
    if not date_text:
        return None
        
    # Clean up the date text
    date_text = date_text.strip()
    
    # Common date formats in these websites
    formats = [
        "%A, %d %B %Y",  # Monday, 01 January 2023
        "%m/%d/%Y",      # 01/01/2023
        "%B %d, %Y",     # January 01, 2023
        "%Y-%m-%d",      # 2023-01-01
        "%d %B %Y",      # 01 January 2023
        "%B %d %Y",      # January 01 2023
        "%m-%d-%Y",      # 01-01-2023
        "%d-%m-%Y"       # 01-01-2023
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue
            
    # Try to extract date using regex as a fallback
    date_patterns = [
        r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})',  # MM/DD/YYYY or DD/MM/YYYY
        r'(\w+)\s+(\d{1,2})[,]?\s+(\d{4})'      # Month DD, YYYY
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, date_text)
        if match:
            try:
                groups = match.groups()
                if len(groups) == 3:
                    # For MM/DD/YYYY pattern
                    if groups[0].isdigit() and groups[1].isdigit():
                        month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
                        if year < 100:
                            year += 2000
                        return datetime(year, month, day)
                    # For Month DD, YYYY pattern
                    else:
                        month_str, day, year = groups
                        month_dict = {
                            'january': 1, 'february': 2, 'march': 3, 'april': 4,
                            'may': 5, 'june': 6, 'july': 7, 'august': 8,
                            'september': 9, 'october': 10, 'november': 11, 'december': 12
                        }
                        month = month_dict.get(month_str.lower())
                        if month:
                            return datetime(int(year), month, int(day))
            except (ValueError, TypeError):
                continue
                
    print(f"⚠️ Could not parse date: '{date_text}'")
    return None

def fetch_bis_federal_register_notices():
    print("Fetching BIS Federal Register notices...")
    driver = setup_driver()
    
    try:
        driver.get("https://www.bis.gov/news-updates/federal-register-notices")
        
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

        data = []

        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 5:
                continue

            pub_date = cols[0].text.strip()
            eff_date = cols[1].text.strip()
            citation = cols[3].text.strip()
            title = cols[4].text.strip()

            # Attempt to extract PDF link
            pdf_link = ""
            try:
                a_tag = cols[5].find_element(By.TAG_NAME, "a")
                pdf_link = a_tag.get_attribute("href") if a_tag else ""
            except:
                pass

            data.append({
                "source": "BIS Federal Register",
                "publication_date": pub_date,
                "effective_date": eff_date,
                "citation": citation,
                "title": title,
                "url": pdf_link
            })

        return data
    
    finally:
        driver.quit()


def fetch_bis_recent_final_rules():
    print("Skipping BIS recent final rules — structure outdated or merged.")
    return []
    
    try:
        # Navigate to the recent final rules page
        driver.get("https://www.bis.doc.gov/index.php/regulations/federal-register-notices#fr-recent-final")
        
        # Wait for the content to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "blog"))
        )
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # The structure here is different, with links inside paragraph elements
        content_div = soup.find("div", class_="blog")
        if not content_div:
            print("⚠️ Could not find 'blog' class. Page structure might have changed.")
            return []
            
        # Look for links in paragraphs
        paragraphs = content_div.find_all("p")
        
        data = []
        for p in paragraphs:
            links = p.find_all("a")
            date_text = None
            
            # Try to extract date from paragraph text before looking at links
            p_text = p.get_text(strip=True)
            date_match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', p_text)
            if date_match:
                date_text = date_match.group(0)
            
            for link in links:
                href = link.get("href")
                if not href or len(href) < 5:  # Skip empty or very short links
                    continue
                    
                title = link.get_text(strip=True)
                if not title or len(title) < 5:  # Skip empty or very short titles
                    continue
                
                # Ensure full URL
                if href.startswith("/"):
                    full_url = "https://www.bis.doc.gov" + href
                else:
                    full_url = href
                
                # Try to extract date from the title if we don't have one yet
                if not date_text:
                    date_match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b', title)
                    if date_match:
                        date_text = date_match.group(0)
                
                date = parse_date(date_text)
                
                # Check if it's a PDF and download it
                pdf_path = None
                if full_url.endswith(".pdf"):
                    pdf_path = download_pdf(full_url)
                
                is_ear_related = "EAR" in title or "Export Administration Regulations" in title
                
                data.append({
                    "source": "BIS Recent Final Rules",
                    "title": title,
                    "url": full_url,
                    "date": date,
                    "is_ear_related": is_ear_related,
                    "pdf_downloaded": bool(pdf_path),
                    "pdf_path": pdf_path
                })
        
        if not data:
            print("⚠️ No articles found in BIS recent rules. Page structure might have changed.")
            
        return data
    
    finally:
        driver.quit()

def fetch_ddtc_updates():
    print("Skipping BIS recent final rules — structure outdated or merged.")
    return []
    # the webpage is deprecated!!
    try:
        # Navigate to the DDTC Updates page
        driver.get("https://www.pmddtc.state.gov/ddtc_public")
        
        # Wait for the content to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "content"))
        )
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # DDTC updates are often in tables or specific divs
        news_items = []
        
        # Look for press releases in the Recent News section
        recent_news = soup.find(lambda tag: tag.name == "h2" and "Recent News" in tag.text if tag.text else False)
        if recent_news:
            news_section = recent_news.find_next("div")
            if news_section:
                news_items = news_section.find_all("div", class_="views-row")
        
        # Look for industry notices
        industry_notices = soup.find(lambda tag: tag.name == "h2" and "Industry Notices" in tag.text if tag.text else False)
        if industry_notices:
            notices_section = industry_notices.find_next("div")
            if notices_section:
                news_items.extend(notices_section.find_all("div", class_="views-row"))
        
        data = []
        for item in news_items:
            title_tag = item.find("h3")
            if not title_tag:
                continue
                
            title = title_tag.get_text(strip=True)
            
            # We'll include all notices, but flag ITAR-specific ones
            is_itar_related = "ITAR" in title or "International Traffic in Arms Regulations" in title
            
            a_tag = title_tag.find("a")
            link = a_tag["href"] if a_tag else "N/A"
            if link and not link.startswith("http"):
                link = "https://www.pmddtc.state.gov" + link
            
            date_tag = item.find("div", class_="views-field-created")
            date_text = date_tag.get_text(strip=True) if date_tag else None
            date = parse_date(date_text)
            
            # Check if it's a PDF and download it
            pdf_path = None
            if link.endswith(".pdf"):
                pdf_path = download_pdf(link)
            
            data.append({
                "source": "DDTC/ITAR Updates",
                "title": title,
                "url": link,
                "date": date,
                "is_itar_related": is_itar_related,
                "pdf_downloaded": bool(pdf_path),
                "pdf_path": pdf_path
            })
        
        if not data:
            print("⚠️ No articles found in DDTC updates. Page structure might have changed.")
            
        return data
    
    finally:
        driver.quit()

def fetch_federal_register_export_controls():
    print("Skipping BIS recent final rules — structure outdated or merged.")
    return []
    # the webpage is deprecated!!
    try:
        # Navigate to the Federal Register Export Controls page
        driver.get("https://www.federalregister.gov/export-controls")
        
        # Wait for the content to load
        WebDriverWait(driver, 15).until(
            # EC.presence_of_element_located((By.CLASS_NAME, "document-list"))
        )
        
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # Find all document items
        items = soup.find_all("li", class_="document-wrapper")
        
        data = []
        for item in items:
            title_tag = item.find("h5")
            if not title_tag:
                continue
                
            title = title_tag.get_text(strip=True)
            
            # Check if it's related to EAR or ITAR
            is_ear_related = "EAR" in title or "Export Administration Regulations" in title
            is_itar_related = "ITAR" in title or "International Traffic in Arms Regulations" in title
            
            a_tag = title_tag.find("a")
            link = a_tag["href"] if a_tag else "N/A"
            
            date_tag = item.find("time")
            date_text = date_tag.get_text(strip=True) if date_tag else None
            date = parse_date(date_text)
            
            # Federal Register items typically don't lead directly to PDFs,
            # but we can check if there's a PDF link in the item
            pdf_link_tag = item.find("a", href=lambda href: href and href.endswith(".pdf"))
            pdf_path = None
            if pdf_link_tag:
                pdf_url = pdf_link_tag["href"]
                pdf_path = download_pdf(pdf_url)
            
            data.append({
                "source": "Federal Register Export Controls",
                "title": title,
                "url": link,
                "date": date,
                "is_ear_related": is_ear_related,
                "is_itar_related": is_itar_related,
                "pdf_downloaded": bool(pdf_path),
                "pdf_path": pdf_path
            })
        
        if not data:
            print("⚠️ No articles found in Federal Register Export Controls. Page structure might have changed.")
            
        return data
    
    finally:
        driver.quit()

def main():
    # Create directories if they don't exist
    os.makedirs("data/raw", exist_ok=True)
    os.makedirs("data/pdfs", exist_ok=True)
    
    # Get data from all sources
    bis_fr_data = fetch_bis_federal_register_notices()
    bis_rules_data = fetch_bis_recent_final_rules()
    ddtc_data = fetch_ddtc_updates()
    fed_register_data = fetch_federal_register_export_controls()
    
    # Combine all data
    all_data = bis_fr_data + bis_rules_data + ddtc_data + fed_register_data
    
    # Create DataFrame
    df = pd.DataFrame(all_data)
    
    # Sort by date if available
    if "date" in df.columns and df["date"].notnull().any():
        df.sort_values(by="date", ascending=False, inplace=True)
    else:
        print("⚠️ Warning: No usable 'date' values found for sorting.")
    
    # Save to CSV
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
    output_file = f"data/raw/export_updates_{timestamp}.csv"
    df.to_csv(output_file, index=False, encoding="utf-8")
    
    print(f"\nData saved to {output_file}")
    print("\nFirst 5 entries:")
    print(df.head())
    
    # Print summary
    print(f"\nSummary:")
    print(f"BIS Federal Register notices: {len(bis_fr_data)}")
    print(f"BIS Recent Final Rules: {len(bis_rules_data)}")
    print(f"DDTC/ITAR Updates: {len(ddtc_data)}")
    print(f"Federal Register Export Controls: {len(fed_register_data)}")
    print(f"Total entries: {len(df)}")
    
    # Count PDFs downloaded
    pdfs_downloaded = sum(1 for item in all_data if item.get("pdf_downloaded", False))
    print(f"PDFs downloaded: {pdfs_downloaded}")
    
    # Count EAR vs ITAR related items
    ear_count = sum(1 for item in all_data if item.get("is_ear_related", False))
    itar_count = sum(1 for item in all_data if item.get("is_itar_related", False))
    print(f"EAR-related items: {ear_count}")
    print(f"ITAR-related items: {itar_count}")

if __name__ == "__main__":
    main()