# Regulus — Export Control Scraper & Change Tracker 🛰️

**Regulus** is a lightweight, extensible Python tool for tracking changes in U.S. export control policy. Built for compliance professionals, policy analysts, and risk managers, Regulus scrapes, parses, and logs policy updates so you don't have to.

---

## 🔍 Purpose

Staying compliant with evolving export controls is mission-critical for firms handling dual-use technologies, semiconductors, and advanced computing.

Regulus automates tracking of:
- 📰 Bureau of Industry and Security (BIS) Federal Register notices  
- 📄 Export Administration Regulations (EAR) rule changes  
- 📦 ECCN category adjustments  
- ⚠️ OFAC sanctions and denied party list entries (planned)

---

## 👥 Who It Helps

- **Classification teams** maintaining up-to-date ECCN documentation  
- **Export compliance officers** detecting emerging regulatory risk  
- **Audit and legal teams** who need versioned logs of rule changes  
- **Policy analysts** tracking semiconductor & trade-related regulatory signals

---

## ⚙️ What It Does

- Scrapes U.S. BIS Federal Register updates (with optional Selenium fallback)
- Downloads and extracts text from linked PDF documents
- Identifies:
  - Entities added to the **Entity List**
  - Key summaries of **final rules**
- Compares new data with prior scans and generates:
  - Excel report with structured results
  - Markdown report highlighting *only* new changes
- Ready to be scheduled via `cron` for regular polling

---

## 🏗️ Project Structure

```text
regulus/
├── scraper/
│   ├── __init__.py
│   ├── bis_scraper.py              # Static BIS HTML scraper
│   ├── bis_selenium.scraper.py     # Selenium-powered BIS scraper (fallback)
│   ├── change_tracker.py           # Compares past vs new updates
│   └── utils.py                    # Common helpers
│
├── data/
│   ├── raw/                        # Raw CSVs from scrape
│   ├── processed/                  # Excel output files
│   └── reports/                    # Markdown change reports
│
├── README.md
├── requirements.txt
├── main.py                         # Primary orchestrator
└── .gitignore
