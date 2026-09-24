# Regulus — Export Control Tracker

**Regulus** is a streamlined Python tool designed to monitor and report changes in U.S. export control regulations. Tailored for compliance professionals, analysts, and policy teams, Regulus automates the tracking of federal register notices, extracts insights from source documents, and highlights new additions over time.

## Current Build

The current stable version of Regulus is [`regulus_v3.py`](regulus_v3.py).

This version pulls new Federal Register documents (BIS, State, OFAC/Treasury) via the Federal Register REST API, scores them for export-control relevance, sends material documents to an LLM for structured compliance analysis, archives everything to SQLite, saves a formatted PDF per alert, and emails material alerts. It also includes an isolated, additive test path (`eccn_test/`) that downloads source PDFs and cross-checks ECCN references via regex against the LLM-derived field.

`regulus.py` (v1.5, Excel-based) and `regulus_v2.py` (v2.2, first API-based rewrite) are retained for reference, tagged [`v2-legacy-selenium-excel`](../../releases/tag/v2-legacy-selenium-excel).

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

