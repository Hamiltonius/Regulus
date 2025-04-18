# Regulus — Export Control Tracker

**Regulus** is a streamlined Python tool designed to monitor and report changes in U.S. export control regulations. Tailored for compliance professionals, analysts, and policy teams, Regulus automates the tracking of federal register notices, extracts insights from source documents, and highlights new additions over time.

## Current Build

The current stable version of Regulus is [`regulus.py`](regulus.py).

This version includes ECCN extraction, PDF analysis, Excel summary output, and a dashboard with guidance tabs for export compliance professionals.

Older builds and experimental files are available in the [`archive/`](archive) folder.

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
regulus/
├── scraper/
│   ├── __init__.py                  # Module initializer
│   ├── bis_scraper.py              # Static HTML scraper for BIS updates
│   ├── bis_scraper2.py             # Secondary scraper (variant/test)
│   ├── change_tracker.py           # Historical diffing & report generation
│   ├── regulus1.2.py               # Archived v1.2 script
│   ├── selenium_scraper.py         # Selenium-based fallback scraper
│   └── utils.py                    # Helper functions
│
│   └── data/
│       ├── pdfs/                   # Downloaded PDFs
│       ├── processed/              # Excel summaries
│       └── raw/                    # Raw CSV outputs
│
├── main.py                         # Optional entrypoint script
├── regulus.py                      # Current production-ready script (v1.5)
├── regulus_scraper.py             # Legacy file with redirect notice
├── requirements.txt
├── README.md
├── .gitignore
└── archive/                        # Archived builds and logs
```

