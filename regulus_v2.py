"""
regulus_v2.py
-------------
BIS Federal Register notice tracker and ECCN extractor.

CHANGELOG v1 -> v2:
- Replaced Selenium/headless Chrome scraper with Federal Register REST API
  (https://www.federalregister.gov/api/v1/documents.json)
- Removed selenium, webdriver, BeautifulSoup dependencies entirely
- Added configurable date range via LOOKBACK_DAYS environment variable
- Added full pagination support (pulls all pages, not just first)
- Fixed chromedriver hardcoded Mac path (was: /opt/homebrew/bin/chromedriver)
- Output paths now configurable via environment variables
- All other pipeline logic (ECCN extraction, PDF download, Excel output) unchanged

CHANGELOG v2.1 (staging for scheduled deployment from agentic cluster):
- Aliased `import pymupdf as fitz` to resolve fitz deprecation warning
- Fixed Excel write crash ("object of type 'float' has no len()") caused by
  NaN values re-read from CSV on subsequent runs; combined_df is now
  sanitized with .fillna("") before being written to Excel
- Replaced fragile whole-dataframe dedup check (df.equals(prev_df), which
  breaks on dtype drift between freshly-parsed and CSV-reloaded columns)
  with a stable set-based comparison on the "url" column

Dependencies: requests, pandas, xlsxwriter, PyMuPDF (imported as pymupdf)
Previously also required: selenium, webdriver-manager, beautifulsoup4 (removed)
"""

import os
import re
import time
import requests
import pymupdf as fitz  # PyMuPDF (aliased for compatibility)
import pandas as pd
from datetime import datetime, timedelta
from urllib.parse import urlparse

# -- Configuration (override via environment variables) ----------------------
LOOKBACK_DAYS   = int(os.environ.get("LOOKBACK_DAYS", 90))
DATA_RAW_DIR    = os.environ.get("DATA_RAW_DIR", "data/raw")
DATA_PDF_DIR    = os.environ.get("DATA_PDF_DIR", "data/pdfs")
DATA_PROC_DIR   = os.environ.get("DATA_PROC_DIR", "data/processed")
MAX_PDF_SIZE_MB = int(os.environ.get("MAX_PDF_SIZE_MB", 5))

# Federal Register API
FR_API_BASE     = "https://www.federalregister.gov/api/v1/documents.json"
BIS_AGENCY_SLUG = "industry-and-security-bureau"
FR_FIELDS       = [
    "title", "publication_date", "effective_on",
    "citation", "pdf_url", "type", "document_number"
]

# ECCN pattern: e.g. 3A090, 5E002, 0A919.a
ECCN_PATTERN = r'\b[0-9][A-Z][0-9]{3}(?:\.[a-z0-9]+)?\b'

# Keyword flags
FLAG_KEYWORDS = [
    "Entity List", "Final Rule", "Huawei",
    "SMIC", "military end use", "PRC"
]

# -- Federal Register API fetch (replaces Selenium scraper) ------------------

def fetch_bis_federal_register_notices(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Fetch BIS Federal Register notices via the FR REST API.

    Args:
        lookback_days: How many days back to pull notices for.

    Returns:
        List of notice dicts matching the schema used by the rest of the pipeline.
    """
    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    print(f"Fetching BIS Federal Register notices since {since_date} via API...")

    param_list = [
        ("conditions[agencies][]", BIS_AGENCY_SLUG),
        ("conditions[publication_date][gte]", since_date),
        ("per_page", 1000),
        ("order", "newest"),
    ]
    for field in FR_FIELDS:
        param_list.append(("fields[]", field))

    all_results = []
    page = 1
    total_pages = None

    while True:
        paged_params = param_list + [("page", page)]
        try:
            response = requests.get(FR_API_BASE, params=paged_params, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            print(f"❌ API request failed (page {page}): {e}")
            break

        results = data.get("results", [])
        if not results:
            break

        if total_pages is None:
            count = data.get("count", 0)
            total_pages = (count + 999) // 1000
            print(f"  → {count} total notices found across {total_pages} page(s).")

        for doc in results:
            pub_date_str = doc.get("publication_date", "")
            eff_date_str = doc.get("effective_on", "") or ""
            all_results.append({
                "source":           "BIS Federal Register",
                "publication_date": pub_date_str,
                "effective_date":   eff_date_str,
                "citation":         doc.get("citation", ""),
                "title":            doc.get("title", ""),
                "url":              doc.get("pdf_url", ""),
                "document_number":  doc.get("document_number", ""),
                "type":             doc.get("type", ""),
                "date":             parse_date(pub_date_str),
                "pdf_downloaded":   False,
                "pdf_path":         None,
            })

        print(f"  → Page {page}/{total_pages}: {len(results)} notices retrieved.")

        if page >= (total_pages or 1):
            break
        page += 1
        time.sleep(0.5)  # Be polite to the API

    print(f"✅ Total notices fetched: {len(all_results)}")
    return all_results


# -- Date parsing (unchanged from v1) -----------------------------------------

def parse_date(date_text: str) -> datetime | None:
    """Parse date string in various formats to datetime object."""
    if not date_text:
        return None

    date_text = date_text.strip()

    formats = [
        "%Y-%m-%d",          # API returns this format: 2025-01-15
        "%A, %d %B %Y",      # Monday, 01 January 2023
        "%m/%d/%Y",          # 01/01/2023
        "%B %d, %Y",         # January 01, 2023
        "%d %B %Y",          # 01 January 2023
        "%B %d %Y",          # January 01 2023
        "%m-%d-%Y",          # 01-01-2023
        "%d-%m-%Y",          # 01-01-2023
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue

    date_patterns = [
        r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})',
        r'(\w+)\s+(\d{1,2})[,]?\s+(\d{4})'
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


# -- PDF download (unchanged from v1) -----------------------------------------

def is_valid_pdf_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc]) and url.lower().endswith('.pdf')
    except Exception:
        return False


def download_pdf(url: str, folder: str = DATA_PDF_DIR) -> str | None:
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


# -- Keyword flagging (unchanged from v1) --------------------------------------

def apply_keyword_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Flag rows containing export control keywords of interest."""
    def find_keywords(text):
        return [kw for kw in FLAG_KEYWORDS if kw.lower() in str(text).lower()]

    df["flagged_keywords"] = df["title"].apply(find_keywords)
    df["flagged"] = df["flagged_keywords"].apply(lambda x: bool(x))
    return df


# -- Excel formatting (unchanged from v1) --------------------------------------

def get_current_quarter() -> str:
    month = datetime.now().month
    quarter = (month - 1) // 3 + 1
    return f"{datetime.now().year}_Q{quarter}"


def format_worksheet(writer, sheet_name: str, df: pd.DataFrame,
                     bold_header: bool = True, autofit: bool = True,
                     row_style: bool = True) -> None:
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    header_format = workbook.add_format({
        "bold": True, "bg_color": "#C4D79B", "border": 1,
        "align": "center", "valign": "vcenter", "text_wrap": True
    })
    cell_format = workbook.add_format({"border": 1, "valign": "top"})

    if sheet_name == "ECCN_Guidance":
        guidance_format = workbook.add_format({"bold": True, "font_size": 14, "text_wrap": True})
        for row_num in range(len(df)):
            worksheet.set_row(row_num, 40, guidance_format)
        worksheet.set_column(0, 0, 150)
        return

    if bold_header:
        worksheet.set_row(0, 42, header_format)
    if autofit:
        for idx, col in enumerate(df.columns):
            max_len = max(df[col].astype(str).map(len).max(), len(col)) + 2
            worksheet.set_column(idx, idx, max_len)
    if row_style:
        for row in range(1, len(df) + 1):
            worksheet.set_row(row, 21, cell_format)


def append_to_master(new_df: pd.DataFrame, processed_dir: str = DATA_PROC_DIR) -> None:
    """Append new data to master file and write formatted Excel report."""
    os.makedirs(processed_dir, exist_ok=True)

    quarter_label     = get_current_quarter()
    master_csv_path   = os.path.join(processed_dir, f"BIS_master_{quarter_label}.csv")
    master_excel_path = os.path.join(processed_dir, f"BIS_master_{quarter_label}.xlsx")

    try:
        if os.path.exists(master_csv_path):
            old_df = pd.read_csv(master_csv_path)
            combined_df = pd.concat([old_df, new_df]).drop_duplicates(subset=["url"])
        else:
            combined_df = new_df
        combined_df.to_csv(master_csv_path, index=False)
        # Sanitize AFTER the CSV write so the CSV itself keeps real NaN values
        # for future dedup/comparison; only the Excel-bound copy gets blanked.
        combined_df = combined_df.fillna("")
    except Exception as e:
        print(f"❌ Error processing master CSV: {e}")
        return

    flagged_df     = combined_df[combined_df["flagged"] == True].copy()
    pdf_summary_df = combined_df[["title", "date", "url"]].copy()
    eccn_summary   = new_df.groupby("publication_date")["eccn_count"].sum().reset_index()
    eccn_summary.columns = ["publication_date", "total_eccns"]

    guidance_text = [
        "📘 ECCN 3A090.a Tracking Guidance",
        "1. Monitor the Federal Register for new and amended ECCNs such as 3A090.a.",
        "2. The Federal Register API (federalregister.gov/api/v1) is the authoritative source used by this tool.",
        "3. Full ECCN definitions live in Supplement No. 1 to Part 774 of the EAR.",
        "4. Filter on phrases like '3A090', 'final rule', or 'model weights' for AI-related controls.",
        "",
        "Note: ECCN 3A090.a controls are associated with AI chipsets and model weights. Updated via interim final rules."
    ]
    df_guidance = pd.DataFrame({"ECCN_Guidance": guidance_text})

    try:
        with pd.ExcelWriter(master_excel_path, engine="xlsxwriter") as writer:
            combined_df.to_excel(writer, sheet_name="all_entries", index=False)
            format_worksheet(writer, "all_entries", combined_df)

            flagged_df.to_excel(writer, sheet_name="flagged_only", index=False)
            format_worksheet(writer, "flagged_only", flagged_df)

            pdf_summary_df.to_excel(writer, sheet_name="pdf_summary", index=False,
                                    startrow=1, header=False)
            worksheet = writer.sheets["pdf_summary"]
            for col_num, header in enumerate(["title", "date", "url"]):
                worksheet.write(0, col_num, header)
            format_worksheet(writer, "pdf_summary", pdf_summary_df)

            df_guidance.to_excel(writer, sheet_name="ECCN_Guidance", index=False)
            format_worksheet(writer, "ECCN_Guidance", df_guidance, row_style=False)

            eccn_summary.to_excel(writer, sheet_name="eccn_summary", index=False)
            format_worksheet(writer, "eccn_summary", eccn_summary)

        print(f"📌 Master updated → {master_csv_path}")
        print(f"📊 Excel export → {master_excel_path}")
    except Exception as e:
        print(f"❌ Error writing Excel file: {e}")


# -- Main pipeline -------------------------------------------------------------

def main() -> None:
    for d in [DATA_RAW_DIR, DATA_PDF_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"❌ Error creating directory {d}: {e}")
            return

    bis_data = fetch_bis_federal_register_notices()

    for item in bis_data:
        item["contains_eccn"] = False
        item["eccn_count"]    = 0
        item["eccns_found"]   = ""

        url = item.get("url")
        if url and url.endswith(".pdf"):
            pdf_path = download_pdf(url)
            item["pdf_downloaded"] = bool(pdf_path)
            item["pdf_path"]       = pdf_path or ""

            if pdf_path:
                try:
                    with fitz.open(pdf_path) as doc:
                        pdf_text = "".join(page.get_text() for page in doc)

                    if not pdf_text.strip():
                        print(f"⚠️ No text extracted from {pdf_path}")

                    eccn_matches = re.findall(ECCN_PATTERN, pdf_text, flags=re.IGNORECASE)
                    unique_eccns = sorted(set(eccn_matches))

                    item["contains_eccn"] = bool(unique_eccns)
                    item["eccn_count"]    = len(unique_eccns)
                    item["eccns_found"]   = ", ".join(unique_eccns)

                    print(f"📄 {pdf_path}: {len(unique_eccns)} ECCNs found ({item['eccns_found']})")
                except Exception as e:
                    print(f"❌ Failed to extract ECCNs from {pdf_path}: {e}")
        else:
            print(f"⚠️ No valid PDF URL for: {item.get('title', 'unknown')}")

    try:
        df = pd.DataFrame(bis_data)
        df = apply_keyword_flags(df)
    except Exception as e:
        print(f"❌ Error creating DataFrame: {e}")
        return

    # Dedup against previous runs — compare on stable "url" identity set,
    # not whole-dataframe equality (which breaks on dtype drift between
    # freshly-parsed columns and columns reloaded from CSV as strings/NaN).
    try:
        previous_files = sorted(
            [f for f in os.listdir(DATA_RAW_DIR) if f.startswith("export_updates_")],
            reverse=True
        )
        if previous_files:
            prev_df = pd.read_csv(os.path.join(DATA_RAW_DIR, previous_files[0]))
            if set(df["url"]) == set(prev_df["url"]):
                print("No new data found since last run.")
                return
    except Exception as e:
        print(f"❌ Error comparing with previous data: {e}")

    if "date" in df.columns and df["date"].notnull().any():
        df.sort_values(by="date", ascending=False, inplace=True)

    try:
        timestamp   = datetime.now().strftime('%Y-%m-%d_%H-%M')
        output_file = os.path.join(DATA_RAW_DIR, f"export_updates_{timestamp}.csv")
        df.to_csv(output_file, index=False, encoding="utf-8")
    except Exception as e:
        print(f"❌ Error saving CSV: {e}")
        return

    append_to_master(df)

    print(f"\nData saved to {output_file}")
    print(f"\nSummary:")
    print(f"  BIS notices retrieved:  {len(bis_data)}")
    print(f"  PDFs downloaded:        {sum(1 for i in bis_data if i.get('pdf_downloaded'))}")
    print(f"  Flagged items:          {df['flagged'].sum()}")
    print(f"  Notices with ECCNs:     {df['contains_eccn'].sum()}")


if __name__ == "__main__":
    main()
