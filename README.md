# Enterprise-Grade Local AI & Document Reporting Platform

A multi-user, RBAC-protected local AI platform built with Streamlit, SQLite, Ollama, and PyPDF.

## Features
- **Government Services & Document Search:** Automatic extraction and searching of ingested PDF documents alongside SQLite tables.
- **Interactive AI Assistant:** Natural language SQL query generation and PDF context-aware document Q&A via Ollama.
- **Audit Logging & Security:** Detailed audit logs, inactivity timeouts, sidebar live countdown timer, and client-side activity tracking.
- **Database Restoration & Validation:** Backup restoration with pre-check validation (`PRAGMA integrity_check` & schema verification).
- **Automated Workflows:** Background scheduler for weekly report generation and email dispatch.

## Requirements
- Python 3.10+
- [Ollama](https://ollama.com) running locally with the `llama3` model (`ollama pull llama3`)

## Quick Start
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run PDF Ingestion (if you have `Downloads.zip` with government PDFs):
   ```bash
   python pdf_ingest.py
   ```
3. Launch the Streamlit application:
   ```bash
   python -m streamlit run app.py
   ```

## Website Sources (admin)
Admins can add website links in the **🌐 Website Sources** tab. The page text is fetched,
split into small sections and stored in the database (`web_sources`, `web_pages` tables).
The chat assistant searches these sections together with the ingested PDFs and mentions the
website URL when it uses one.

- Works with normal web pages, PDF links and plain-text pages.
- Pages that need JavaScript to show their content cannot be read.
- Use 🔄 to fetch a page again after it changes, 🗑️ to remove it.
- Security: only public http/https addresses are allowed (localhost and private network
  addresses are blocked), pages over 5 MB are refused, and every change is written to `audit_logs`.
- Analysts can ask questions about the websites but cannot add or remove them.

## Image & PDF Library (admin)
Admins add images (PNG, JPG, WEBP, GIF) and PDF files in the **🖼️ Image & PDF Library** tab.
Every image and every PDF page is stored in the database as a picture plus searchable text:
the admin's description/keywords, text extracted from PDF pages, and (optionally) an AI
description made by a local vision model.

In the chat, ask for what you want to see, e.g. *"show me the Lotus Tower"* or
*"show the passport application form"*. The assistant searches the library and displays the
matching pictures (up to 3) under its answer. A picture is also shown without asking when
its title/description clearly matches the question.

- **Scanned PDFs / pictures with no text:** click 🤖 *Describe* so a vision model can describe
  them and read their text. One-time setup: `ollama pull llava` (smaller: `ollama pull moondream`).
  Up to 30 items are processed per click.
- Limits: images 10 MB, PDFs 25 MB / 150 pages. Duplicates are skipped.
- Needs `pypdfium2` and `Pillow` (in `requirements.txt`) to show PDF pages as pictures.
- Analysts can ask the chat to show library files but cannot add, edit or delete them.
- The AI can never read the internal tables (audit log, library, website data) through SQL.
