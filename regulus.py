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
import fitz  # PyMuPDF

def setup_driver():
    """Set up and configure Chrome WebDriver for headless operation."""
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    service = Service("/opt/homebrew/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    return driver

MAX_PDF_SIZE_MB = 5

from urllib.parse import urlparse

def is_valid_pdf_url(url):
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc]) and url.lower().endswith('.pdf')
    except Exception:
        return False

def download_pdf(url, folder="data/pdfs"):
    """Download a PDF from a validated URL with size and content-type checks."""
    if not is_valid_pdf_url(url):
        print(f"❌ Skipped invalid URL: {url}")
        return None

    try:
        head = requests.head(url, timeout=5, allow_redirects=True)
        content_type = head.headers.get("Content-Type", "")
        size_bytes = int(head.headers.get("Content-Length", 0))
        if size_bytes > MAX_PDF_SIZE_MB * 1024 * 1024:
            print(f"⚠️ Skipped large file ({size_bytes/1e6:.2f} MB): {url}")
            return None
        if 'application/pdf' not in content_type.lower():
            print(f"❌ Skipped non-PDF content type ({content_type}): {url}")
            return None

        response = requests.get(url, stream=True, timeout=10, allow_redirects=True)
        if response.status_code == 200:
            os.makedirs(folder, exist_ok=True)
            filename = os.path.basename(urlparse(url).path) or f"pdf_{int(time.time())}.pdf"
            output_path = os.path.join(folder, filename)
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"✅ PDF saved: {output_path}")
            return output_path
        else:
            print(f"❌ HTTP error {response.status_code}: {url}")
            return None
    except Exception as e:
        print(f"❌ Failed to download {url}: {e}")
        return None

def parse_date(date_text):
    """Parse date text in various formats to datetime object.
    
    Args:
        date_text: String containing a date
        
    Returns:
        datetime object or None if parsing failed
    """
    if not date_text:
        return None
        
    date_text = date_text.strip()
    
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
                    if groups[0].isdigit() and groups[1].isdigit():
                        month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
                        if year < 100:
                            year += 2000
                        return datetime(year, month, day)
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
    """Fetch BIS Federal Register notices from the official website.
    
    Returns:
        List of dictionaries with notice information
    """
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
            date_obj = parse_date(pub_date)
            eff_date = cols[1].text.strip()
            citation = cols[3].text.strip()
            title = cols[4].text.strip()

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
                "url": pdf_link,
                "date": date_obj,
                "pdf_downloaded": False,
                "pdf_path": None
            })

        return data
    
    finally:
        driver.quit()

def get_current_quarter():
    """Return current quarter string, e.g., '2024_Q2'"""
    month = datetime.now().month
    quarter = (month - 1) // 3 + 1
    return f"{datetime.now().year}_Q{quarter}"

def format_worksheet(writer, sheet_name, df, bold_header=True, autofit=True, row_style=True):
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    header_format = workbook.add_format({
        "bold": True,
        "bg_color": "#C4D79B",
        "border": 1,
        "align": "center",
        "valign": "vcenter",
        "text_wrap": True
    })
    cell_format = workbook.add_format({"border": 1, "valign": "top"})

    # Special styling for ECCN Guidance tab
    if sheet_name == "ECCN_Guidance":
        guidance_format = workbook.add_format({
            "bold": True,
            "font_size": 14,
            "text_wrap": True
        })
        for row_num in range(len(df)):
            worksheet.set_row(row_num, 40, guidance_format)  # Uniform height of 40
        worksheet.set_column(0, 0, 150)
        return

    # Regular formatting
    if bold_header:
        worksheet.set_row(0, 42, header_format)
    if autofit:
        for idx, col in enumerate(df.columns):
            max_len = max(df[col].astype(str).map(len).max(), len(col)) + 2
            worksheet.set_column(idx, idx, max_len)
    if row_style:
        for row in range(1, len(df) + 1):
            worksheet.set_row(row, 21, cell_format)

def append_to_master(new_df, processed_dir="data/processed"):
    """Append new data to master file and create formatted Excel report."""
    os.makedirs(processed_dir, exist_ok=True)

    quarter_label = get_current_quarter()
    master_csv_path = os.path.join(processed_dir, f"BIS_master_{quarter_label}.csv")
    master_excel_path = os.path.join(processed_dir, f"BIS_master_{quarter_label}.xlsx")

    try:
        if os.path.exists(master_csv_path):
            old_df = pd.read_csv(master_csv_path)
            combined_df = pd.concat([old_df, new_df]).drop_duplicates(subset=["url"])
        else:
            combined_df = new_df

        combined_df.to_csv(master_csv_path, index=False)
    except Exception as e:
        print(f"❌ Error processing master CSV: {e}")
        return

    flagged_df = combined_df[combined_df["flagged"] == True].copy()
    pdf_summary_df = combined_df[["title", "date", "url"]].copy()

    guidance_text = [
        "📘 ECCN 3A090.a Tracking Guidance",
        "1. Monitor the Federal Register for new and amended ECCNs such as 3A090.a. For example: https://www.federalregister.gov/documents/2025/01/15/2025-00636/framework-for-artificial-intelligence-diffusion.",
        "2. BIS occasionally posts summary pages related to AI export policy and license diffusion (e.g., bis.gov/AI-diffusion), but the Federal Register is the authoritative source.",
        "3. Full ECCN definitions live in Supplement No. 1 to Part 774 of the EAR. For licensing notes (like Note 1), refer to § 742.6(a)(6)(iii)(A).",
        "4. Consider using the Federal Register API or scraping pdfs for phrases like '3A090', 'final rule', or 'model weights' to detect new AI-related controls.",
        "",
        "Note: ECCN 3A090.a controls are often associated with AI chipsets and model weights for closed-weight dual-use AI systems. These rules are updated via interim final rules and are time-sensitive."
    ]
    df_guidance = pd.DataFrame({"ECCN_Guidance": guidance_text})
    
    eccn_summary = new_df.groupby("publication_date")["eccn_count"].sum().reset_index()
    eccn_summary.columns = ["publication_date", "total_eccns"]
    
    try:
        with pd.ExcelWriter(master_excel_path, engine="xlsxwriter") as writer:
            combined_df.to_excel(writer, sheet_name="all_entries", index=False)
            format_worksheet(writer, "all_entries", combined_df)

            flagged_df.to_excel(writer, sheet_name="flagged_only", index=False)
            format_worksheet(writer, "flagged_only", flagged_df)

            pdf_summary_df.to_excel(writer, sheet_name="pdf_summary", index=False, startrow=1, header=False)
            worksheet = writer.sheets["pdf_summary"]
            for col_num, header in enumerate(["title", "date", "url"]):
                worksheet.write(0, col_num, header)
            format_worksheet(writer, "pdf_summary", pdf_summary_df)

            df_guidance.to_excel(writer, sheet_name="ECCN_Guidance", index=False)
            format_worksheet(writer, "ECCN_Guidance", df_guidance, row_style=False)

            eccn_summary.to_excel(writer, sheet_name="eccn_summary", index=False)
            format_worksheet(writer, "eccn_summary", eccn_summary)

        print(f"📌 Master updated → {master_csv_path}")
        print(f"📊 Excel export complete → {master_excel_path} with clickable links and ECCN guidance tab.")
    except Exception as e:
        print(f"❌ Error writing Excel file: {e}")

def apply_keyword_flags(df):
    """Flag rows containing specific keywords.
    
    Args:
        df: DataFrame to process
        
    Returns:
        DataFrame with added flagged_keywords and flagged columns
    """
    keywords = ["Entity List", "Final Rule", "Huawei", "SMIC", "military end use", "PRC"]
    
    def find_keywords(text):
        matches = [kw for kw in keywords if kw.lower() in str(text).lower()]
        return matches

    df["flagged_keywords"] = df["title"].apply(find_keywords)
    df["flagged"] = df["flagged_keywords"].apply(lambda x: bool(x))
    return df

def main():
    """Main function to execute the web scraping and report generation."""
    try:
        os.makedirs("data/raw", exist_ok=True)
        os.makedirs("data/pdfs", exist_ok=True)
    except OSError as e:
        print(f"❌ Error creating directories: {e}")
        return

    bis_data = fetch_bis_federal_register_notices()
    
    for item in bis_data:
        item["contains_eccn"] = False
        item["eccn_count"] = 0
        item["eccns_found"] = ""

        url = item.get("url")
        if url and url.endswith(".pdf"):
            pdf_path = download_pdf(url)
            item["pdf_downloaded"] = bool(pdf_path)
            item["pdf_path"] = pdf_path or ""

            if pdf_path:
                try:
                    with fitz.open(pdf_path) as doc:
                        pdf_text = ""
                        for page in doc:
                            pdf_text += page.get_text()
                    
                    if not pdf_text.strip():
                        print(f"⚠️ No text extracted from {pdf_path}")
                    
                    eccn_pattern = r'\b[0-9][A-Z][0-9]{3}(?:\.[a-z0-9]+)?\b'
                    eccn_matches = re.findall(eccn_pattern, pdf_text, flags=re.IGNORECASE)
                    unique_eccns = sorted(set(eccn_matches))
                    
                    item["contains_eccn"] = bool(unique_eccns)
                    item["eccn_count"] = len(unique_eccns)
                    item["eccns_found"] = ", ".join(unique_eccns)
                    
                    print(f"📄 Processed {pdf_path}: {len(unique_eccns)} ECCNs found ({item['eccns_found']})")
                except Exception as e:
                    print(f"❌ Failed to extract ECCNs from {pdf_path}: {e}")
        else:
            print(f"⚠️ No valid PDF URL for {item.get('title', 'unknown')}: {url}")

    try:
        df = pd.DataFrame(bis_data)
        df = apply_keyword_flags(df)
    except Exception as e:
        print(f"❌ Error creating DataFrame: {e}")
        return

    output_dir = "data/raw"
    try:
        previous_files = sorted(
            [f for f in os.listdir(output_dir) if f.startswith("export_updates_")],
            reverse=True
        )
        if previous_files:
            prev_path = os.path.join(output_dir, previous_files[0])
            prev_df = pd.read_csv(prev_path)
            if df.equals(prev_df):
                print("No new data found since last run.")
                return
    except Exception as e:
        print(f"❌ Error comparing with previous data: {e}")

    if "date" in df.columns and df["date"].notnull().any():
        df.sort_values(by="date", ascending=False, inplace=True)

    try:
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
        output_file = f"data/raw/export_updates_{timestamp}.csv"
        df.to_csv(output_file, index=False, encoding="utf-8")
    except Exception as e:
        print(f"❌ Error saving CSV: {e}")
        return
    
    append_to_master(df)
    
    print(f"\nData saved to {output_file}")
    print("\nFirst 5 entries:")
    print(df.head())
    
    print(f"\nSummary:")
    print(f"BIS Federal Register notices: {len(bis_data)}")
    print(f"Total entries: {len(df)}")
    
    pdfs_downloaded = sum(1 for item in bis_data if item.get("pdf_downloaded", False))
    print(f"PDFs downloaded: {pdfs_downloaded}")
    
    flagged_count = df["flagged"].sum()
    print(f"Flagged items: {flagged_count}")

if __name__ == "__main__":
    main()