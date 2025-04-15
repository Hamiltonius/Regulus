# regulus_scraper.py
# Description: Extracts, analyzes, and tracks BIS/ITAR export control updates

import os
import re
import pandas as pd
import requests
import logging
import argparse
from datetime import datetime
from scraper.selenium_scraper import main as run_scraper
import fitz  # PyMuPDF

# Globals to collect extracted info
entity_additions = []
final_rule_notes = []

def setup_logging():
    """Configure logging to file and console"""
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y-%m-%d')
    log_file = f"{log_dir}/regulus_{timestamp}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="BIS/ITAR Export Control Update Scanner")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip website scraping and use existing CSV")
    parser.add_argument("--output-dir", default="data/processed", help="Directory for output files")
    parser.add_argument("--report-dir", default="data/reports", help="Directory for change reports")
    return parser.parse_args()

def extract_text_from_pdf(url):
    """Download PDF from URL and extract text"""
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            logging.error(f"Unable to download {url}: Status code {response.status_code}")
            return f"ERROR: Unable to download {url}"

        # Use a unique temp file to avoid conflicts in concurrent processing
        temp_file = f"temp_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
        
        try:
            with open(temp_file, "wb") as f:
                f.write(response.content)

            with fitz.open(temp_file) as doc:
                return "\n".join(page.get_text() for page in doc)
        finally:
            # Always clean up the temp file
            if os.path.exists(temp_file):
                os.remove(temp_file)

    except Exception as e:
        logging.error(f"Error processing {url}: {str(e)}")
        return f"ERROR: {str(e)}"

def extract_insights(text, url):
    """Extract key information from PDF text"""
    entity_patterns = [
        r'(?:added|adding|placed)(?:\s+\w+){0,6}\s+to the Entity List[^.]*?(?:in|from|for)\s+([^,\n.]+)',
        r'Entity List[^.]*?(?:in|from|for)\s+([^,\n.]+)',
        r'(?:added|adding|placed)(?:\s+\w+){0,6}\s+to[^.]*?(?:Entity List)[^.]*?(?:in|from|for)\s+([^,\n.]+)'
    ]
    
    rule_patterns = [
        r'(?:This|The) (?:final|interim final) rule (?:makes|revises|amends|modifies)[^.]+',
        r'(?:This|The) rule (?:implements|establishes|removes|adds)[^.]+',
        r'Purpose of (?:this|the) rule:[^.]+',
        r'SUMMARY:[^\n]+(?:final rule)[^\n]+'
    ]
    
    # Use sets to track already added items to avoid duplicates
    found_entities = set()
    found_rules = set()
    
    for pattern in entity_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            clean_entity = m.strip()
            if clean_entity and clean_entity not in found_entities:
                entity_additions.append({'url': url, 'region_or_country': clean_entity})
                found_entities.add(clean_entity)

    for pattern in rule_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        for m in matches:
            clean_rule = m.strip()
            if clean_rule and clean_rule not in found_rules:
                final_rule_notes.append({'url': url, 'final_rule_text': clean_rule})
                found_rules.add(clean_rule)

def get_latest_csv(data_dir="data/raw"):
    """Find the most recent CSV file in the data directory"""
    try:
        csvs = [f for f in os.listdir(data_dir) if f.endswith(".csv")]
        if not csvs:
            raise FileNotFoundError(f"No CSV files found in {data_dir}")
        return os.path.join(data_dir, sorted(csvs)[-1])
    except Exception as e:
        logging.error(f"Error finding latest CSV: {str(e)}")
        raise

def generate_change_report(current_entities, current_rules, report_dir="data/reports"):
    """Generate a report highlighting changes from previous scan"""
    try:
        processed_dir = "data/processed"
        os.makedirs(report_dir, exist_ok=True)
        
        excel_files = [f for f in os.listdir(processed_dir) if f.endswith(".xlsx")]
        if len(excel_files) < 2:
            logging.info("Not enough history for diff report - need at least two scans")
            return
            
        # Sort to get the previous file (newest will be the one we just created)
        excel_files.sort()
        previous_file = os.path.join(processed_dir, excel_files[-2])
        
        logging.info(f"Comparing with previous report: {previous_file}")
        
        # Load previous data
        prev_entities = pd.read_excel(previous_file, sheet_name="Entity Additions")
        prev_rules = pd.read_excel(previous_file, sheet_name="Final Rule Notes")
        
        # Convert current data to DataFrames
        current_entities_df = pd.DataFrame(current_entities)
        current_rules_df = pd.DataFrame(current_rules)
        
        # Find new entries by comparing with previous data
        if not current_entities_df.empty and not prev_entities.empty:
            new_entities = current_entities_df[~current_entities_df['region_or_country'].isin(prev_entities['region_or_country'])]
        else:
            new_entities = current_entities_df
            
        if not current_rules_df.empty and not prev_rules.empty:
            # Use first 50 chars as a simple way to compare rule text
            new_rules = current_rules_df[~current_rules_df['final_rule_text'].str[:50].isin(prev_rules['final_rule_text'].str[:50])]
        else:
            new_rules = current_rules_df

        # Generate the report file
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
        report_path = f"{report_dir}/changes_report_{timestamp}.md"
        
        with open(report_path, "w") as f:
            f.write(f"# Export Control Changes Report\n\n")
            f.write(f"**Generated:** {timestamp}\n\n")
            
            f.write("## New Entity Additions\n\n")
            if not new_entities.empty:
                for idx, (_, row) in enumerate(new_entities.iterrows(), 1):
                    f.write(f"{idx}. **{row['region_or_country']}**\n")
                    f.write(f"   - [Source Document]({row['url']})\n\n")
            else:
                f.write("*No new entity additions detected since last scan.*\n\n")
            
            f.write("## New Final Rules\n\n")
            if not new_rules.empty:
                for idx, (_, row) in enumerate(new_rules.iterrows(), 1):
                    # Truncate rule text for better readability
                    rule_summary = row['final_rule_text'][:100] + "..." if len(row['final_rule_text']) > 100 else row['final_rule_text']
                    f.write(f"{idx}. **{rule_summary}**\n")
                    f.write(f"   - [Full Document]({row['url']})\n\n")
            else:
                f.write("*No new final rules detected since last scan.*\n\n")

        logging.info(f"✅ Change report saved: {report_path}")
        return report_path
        
    except Exception as e:
        logging.error(f"⚠️ Error generating change report: {str(e)}")
        return None

def main():
    """Main execution function"""
    # Parse command line arguments
    args = parse_args()
    
    # Set up logging
    setup_logging()
    
    # Ensure output directories exist
    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(args.report_dir, exist_ok=True)
    os.makedirs("data/raw", exist_ok=True)
    
    logging.info("🚀 Starting BIS/ITAR export control scanner")
    
    # Step 1: Run web scraper unless skipped
    if not args.skip_scrape:
        logging.info("Running web scraper to collect updates...")
        run_scraper()
    else:
        logging.info("Skipping web scrape as requested")
    
    # Step 2: Get the latest CSV file
    try:
        latest_csv = get_latest_csv()
        logging.info(f"Using data from: {latest_csv}")
        
        # Step 3: Process the CSV data
        df = pd.read_csv(latest_csv)
        logging.info(f"📊 Loaded {len(df)} entries from CSV")
        
        # Clear global containers
        entity_additions.clear()
        final_rule_notes.clear()
        
        # Prepare container for summary
        summary_rows = []
        
        # Process each URL in the CSV
        for idx, row in enumerate(df.iterrows(), 1):
            _, data = row
            url = data.get("url")
            
            if isinstance(url, str) and url.endswith(".pdf"):
                logging.info(f"🔍 Processing PDF {idx}/{len(df)}: {url}")
                text = extract_text_from_pdf(url)
                
                if not text.startswith("ERROR"):
                    extract_insights(text, url)
                else:
                    logging.warning(f"Could not process {url}: {text}")
            
            summary_rows.append(data.to_dict())
        
        # Step 4: Save results to Excel file
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
        output_path = f"{args.output_dir}/export_insights_{timestamp}.xlsx"
        
        with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
            pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
            pd.DataFrame(entity_additions).to_excel(writer, sheet_name="Entity Additions", index=False)
            pd.DataFrame(final_rule_notes).to_excel(writer, sheet_name="Final Rule Notes", index=False)
        
        logging.info(f"✅ Results exported to: {output_path}")
        
        # Print summary statistics
        print("\n📊 Processing Summary:")
        print(f"- URLs processed: {len(df)}")
        print(f"- Entity additions found: {len(entity_additions)}")
        print(f"- Final rule notes extracted: {len(final_rule_notes)}")
        
        # Step 5: Generate change report
        report_path = generate_change_report(entity_additions, final_rule_notes, args.report_dir)
        
        if report_path:
            print(f"\n📋 Change report saved to: {report_path}")
        
        logging.info("✅ Export control scan completed successfully")
        
    except Exception as e:
        logging.error(f"❌ Error in main execution: {str(e)}")
        raise

if __name__ == "__main__":
    main()
