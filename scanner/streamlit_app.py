import streamlit as st
import os
from pathlib import Path
from test2 import EmailScanner

st.set_page_config(page_title="Email Scanner UI", layout="wide")
st.title("Reactive Email Scanner")

# Target input on main page (prominent)
with st.form(key='scan_form'):
    target = st.text_input("Target website URL (include https://)")
    st.write("")
    submit_button = st.form_submit_button("Submit")

with st.sidebar:
    st.header("Scan options")
    render_js = st.checkbox("Render JavaScript (Selenium)", value=False)
    workers = st.number_input("Worker threads", min_value=1, max_value=32, value=8)
    render_wait = st.number_input("Render wait (seconds)", min_value=0, max_value=10, value=2)
    max_depth = st.number_input("Max crawl depth", min_value=0, max_value=5, value=2)
    max_pages = st.number_input("Max pages to scan", min_value=1, max_value=1000, value=20)
    sequential = st.checkbox("Run sequential crawler (no reactive)", value=False)
    start_button = st.button("Start Scan")

output_area = st.empty()
results_area = st.empty()

if start_button or submit_button:
    if not target:
        st.error("Please enter a target URL.")
    else:
        # normalize target
        if target.lower().startswith('target='):
            target = target.split('=', 1)[1].strip()
        if (target.startswith("'") and target.endswith("'")) or (target.startswith('"') and target.endswith('"')):
            target = target[1:-1].strip()
        if not target.startswith('http'):
            target = 'https://' + target

        scanner = EmailScanner(target,
                               max_depth=int(max_depth),
                               max_pages=int(max_pages),
                               render_js=bool(render_js),
                               render_wait=int(render_wait))

        with st.spinner('Scanning — this may take a while...'):
            try:
                if sequential:
                    scanner.crawl()
                else:
                    scanner.crawl_rx(max_workers=int(workers))
            except Exception as e:
                st.error(f"Scan failed: {e}")

        # show results
        emails = list(scanner.emails_found.keys())
        if emails:
            results_area.subheader(f"Found {len(emails)} unique emails")
            results_area.table([{"email": e, "source": scanner.emails_found[e]} for e in emails])

            # ensure files exist
            csv_path = Path('audit_report.csv')
            txt_path = Path('top_emails.txt')

            if csv_path.exists():
                with open(csv_path, 'rb') as f:
                    csv_data = f.read()
                st.download_button("Download CSV report", data=csv_data, file_name='audit_report.csv', mime='text/csv')

            # create or read top_emails
            if txt_path.exists():
                with open(txt_path, 'rb') as f:
                    txt_data = f.read()
                st.download_button("Download emails (txt)", data=txt_data, file_name='top_emails.txt', mime='text/plain')
        else:
            results_area.info("No emails found.")

st.sidebar.markdown("---")
st.sidebar.markdown("Requirements: streamlit, rx, selenium (optional), webdriver-manager.")
st.sidebar.markdown("Run: streamlit run streamlit_app.py")
