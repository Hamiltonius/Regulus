# Regulus — Export Control Tracker

**Regulus** is a streamlined Python tool designed to monitor and report changes in U.S. export control regulations. Tailored for compliance professionals, analysts, and policy teams, Regulus automates the tracking of federal register notices, extracts insights from source documents, and highlights new additions over time.

---

## Purpose

Export control compliance is critical for organizations working with dual-use technologies, semiconductors, and sensitive trade items. Regulus helps teams stay ahead by automating the monitoring of:

- Bureau of Industry and Security (BIS) Federal Register notices  
- Export Administration Regulations (EAR) rule updates  
- ECCN classification changes  
- *(Planned)* OFAC sanctions and denied party list entries  

---

## Who It's For

- **Export compliance teams** maintaining accurate, up-to-date documentation  
- **Classification groups** managing ECCN tracking and reporting  
- **Audit/legal departments** needing versioned compliance logs  
- **Policy analysts** following regulatory shifts in technology and trade  

---

## Key Features

- Scrapes BIS Federal Register updates, with Selenium fallback for complex pages  
- Extracts text from PDFs linked in register entries  
- Applies regex-based parsing to detect:
  - Entity List additions  
  - Final rule summaries  
- Compares current results against previous scans  
- Outputs:
  - Excel reports with tabbed summaries  
  - Markdown reports highlighting new regulatory changes  
- Can be run manually or integrated into a scheduled `cron` job  

---

## Project Structure

```
regulus/ ├── scraper/ │ ├── bis_scraper.py # Static HTML scraper │ ├── bis_selenium.scraper.py # Selenium fallback for JavaScript pages │ ├── change_tracker.py # Compares current vs. historical entries │ └── utils.py # Helper methods │ ├── data/ │ ├── raw/ # Raw CSVs from scrape │ ├── processed/ # Excel output files │ └── reports/ # Markdown diffs for easy review │ ├── main.py # Execution entrypoint ├── regulus_scraper.py # Full PDF parser + change tracker ├── requirements.txt ├── README.md └── .gitignore
```

regulus/
├── scraper/
│   ├── __init__.py
│   ├── bis_scraper.py              # Static HTML scraper for BIS updates
│   ├── bis_selenium.scraper.py     # Selenium-based fallback scraper
│   ├── change_tracker.py           # Historical diffing & report generation
│   └── utils.py                    # Helper functions
│
├── data/
│   ├── raw/                        # Raw CSV outputs
│   ├── processed/                  # Excel summaries
│   └── reports/                    # Markdown diffs
│
├── main

