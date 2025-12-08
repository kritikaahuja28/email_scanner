import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, urljoin
import time
from collections import deque
import urllib.robotparser
import csv
import urllib3
import threading
import rx
from rx import operators as ops
from rx.subject import Subject
from rx.scheduler import ThreadPoolScheduler
from concurrent.futures import ThreadPoolExecutor
import sys

# Suppress SSL warnings for cleaner output since we are scanning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class EmailScanner:
    def __init__(self, start_url, max_depth=2, max_pages=20, render_js=False, render_wait=2):
        self.start_url = start_url
        self.domain = urlparse(start_url).netloc
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.visited_urls = set()
        # Map Email -> Source URL
        self.emails_found = {}
        self.urls_to_visit = deque([(start_url, 0)])
        
        # Robot Parser setup
        self.rp = urllib.robotparser.RobotFileParser()
        self.robots_url = urljoin(start_url, "/robots.txt")
        self.rp.set_url(self.robots_url)
        self.robots_parsed = False
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }

        # JS rendering options
        self.render_js = render_js
        self.render_wait = render_wait
        self._selenium_available = None

        # Concurrency helpers for reactive crawl
        self.lock = threading.Lock()
        self.pages_scanned = 0

    def _try_import_selenium(self):
        """Try to import selenium and webdriver_manager. Cache result in self._selenium_available."""
        if self._selenium_available is not None:
            return self._selenium_available
        try:
            from selenium import webdriver  # noqa: F401
            from selenium.webdriver.chrome.options import Options  # noqa: F401
            from webdriver_manager.chrome import ChromeDriverManager  # noqa: F401
            self._selenium_available = True
        except Exception:
            self._selenium_available = False
        return self._selenium_available

    def fetch_page(self, url):
        """Fetch page HTML. Use requests by default, or Selenium if render_js=True and available.
        Returns a tuple (html_text, status_code).
        """
        # Prefer requests for speed
        if not self.render_js:
            try:
                resp = requests.get(url, headers=self.headers, timeout=10, verify=False)
                return resp.text, resp.status_code
            except Exception:
                return "", 0

        # If we need JS rendering
        if not self._try_import_selenium():
            # fallback to requests if selenium is not available
            try:
                resp = requests.get(url, headers=self.headers, timeout=10, verify=False)
                return resp.text, resp.status_code
            except Exception:
                return "", 0

        # Use selenium to render
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager

            options = Options()
            # Use modern headless flag for compatibility; keep other flags
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(f"user-agent={self.headers['User-Agent']}")

            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(30)
            driver.get(url)
            time.sleep(self.render_wait)
            rendered = driver.page_source
            status_code = 200
            driver.quit()
            return rendered, status_code
        except Exception:
            # fallback to requests on any selenium failure
            try:
                resp = requests.get(url, headers=self.headers, timeout=10, verify=False)
                return resp.text, resp.status_code
            except Exception:
                return "", 0

    def check_robots_txt(self):
        """Reads robots.txt to respect site rules."""
        print("[*] Checking robots.txt...")
        try:
            # Use requests with verify=False to bypass SSL errors
            response = requests.get(self.robots_url, headers=self.headers, timeout=10, verify=False)
            
            if response.status_code == 200:
                # Pass the lines to the parser manually
                self.rp.parse(response.text.splitlines())
                self.robots_parsed = True
                
                if self.rp.can_fetch(self.headers['User-Agent'], self.start_url):
                    print("[+] Allowed to crawl according to robots.txt")
                    return True
                else:
                    print("[-] Disallowed by robots.txt")
                    return False
            else:
                print(f"[!] robots.txt not found (Status: {response.status_code}). Proceeding.")
                return True
                
        except Exception as e:
            print(f"[!] Could not fetch robots.txt (proceeding with caution): {e}")
            return True # Default to allow if robots.txt is missing/error

    def is_valid_url(self, url):
        """Checks if the URL is valid and belongs to the target domain."""
        parsed = urlparse(url)
        return bool(parsed.netloc) and parsed.netloc == self.domain

    def extract_emails(self, text):
        """Extracts emails from text using regex."""
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        return set(re.findall(email_pattern, text))

    def crawl(self):
        # keep the original sequential crawler for compatibility
        # Initial check for robots.txt
        if not self.check_robots_txt():
            print("[-] Aborting scan due to robots.txt restrictions.")
            return

        print(f"[*] Starting scan of {self.start_url}")
        print(f"[*] Max depth: {self.max_depth}, Max pages: {self.max_pages}")
        
        pages_scanned = 0

        while self.urls_to_visit and pages_scanned < self.max_pages:
            current_url, depth = self.urls_to_visit.popleft()

            if current_url in self.visited_urls:
                continue

            if depth > self.max_depth:
                continue

            # Check robots.txt for this specific URL if we successfully parsed it
            if self.robots_parsed and not self.rp.can_fetch(self.headers['User-Agent'], current_url):
                print(f"[-] Skipped (robots.txt): {current_url}")
                continue

            print(f"Processing: {current_url}")
            
            try:
                # Fetch page (requests or Selenium-rendered HTML)
                html_text, status = self.fetch_page(current_url)
                # mark visited immediately to avoid repeats
                self.visited_urls.add(current_url)
                pages_scanned += 1

                # Parse fetched HTML
                if status != 200:
                    continue

                soup = BeautifulSoup(html_text, 'html.parser')

                # 1. Extract emails and map them to the current URL
                found_on_page = set()
                
                # Text content
                found_on_page.update(self.extract_emails(soup.get_text()))
                
                # Mailto links (case-insensitive and tolerant)
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    href_lower = href.lower()
                    if 'mailto:' in href_lower:
                        try:
                            email = href_lower.split('mailto:', 1)[1].split('?')[0].strip()
                            found_on_page.add(email)
                        except Exception:
                            pass

                # Store findings
                for email in found_on_page:
                    if email not in self.emails_found:
                        self.emails_found[email] = current_url
                        print(f"    [!] Found: {email}")

                # 2. Find new links to visit
                if depth < self.max_depth:
                    for link in soup.find_all('a', href=True):
                        href = link['href']
                        full_url = urljoin(current_url, href)
                        
                        if self.is_valid_url(full_url) and full_url not in self.visited_urls:
                            self.urls_to_visit.append((full_url, depth + 1))
                
                time.sleep(1) # Be polite

            except requests.RequestException as e:
                print(f"[-] Error scanning {current_url}: {e}")
            except KeyboardInterrupt:
                print("\n[!] Scan interrupted by user.")
                break

        self.save_report(pages_scanned)

    def crawl_rx(self, max_workers=8):
        """Reactive crawler using RxPY and a thread pool."""
        if not self.check_robots_txt():
            print("[-] Aborting scan due to robots.txt restrictions.")
            return

        print(f"[*] Starting reactive scan of {self.start_url}")
        print(f"[*] Max depth: {self.max_depth}, Max pages: {self.max_pages}, workers: {max_workers}")

        executor = ThreadPoolExecutor(max_workers=max_workers)
        scheduler = ThreadPoolScheduler(max_workers)

        url_subject = Subject()
        done_event = threading.Event()

        def process(item):
            url, depth = item
            with self.lock:
                if url in self.visited_urls or depth > self.max_depth or self.pages_scanned >= self.max_pages:
                    return {'url': url, 'emails': set(), 'links': [], 'skipped': True}
                # reserve this URL
                self.visited_urls.add(url)
            # fetch and parse
            html, status = self.fetch_page(url)
            if status != 200:
                return {'url': url, 'emails': set(), 'links': [], 'skipped': False}
            soup = BeautifulSoup(html, 'html.parser')
            found = set(self.extract_emails(soup.get_text()))
            for a in soup.find_all('a', href=True):
                href = a['href']
                if 'mailto:' in href.lower():
                    try:
                        found.add(href.split(':', 1)[1].split('?')[0].strip())
                    except Exception:
                        pass

            links = []
            if depth < self.max_depth:
                for a in soup.find_all('a', href=True):
                    full = urljoin(url, a['href'])
                    with self.lock:
                        if self.is_valid_url(full) and full not in self.visited_urls:
                            # don't mark visited here; let process() reserve when it runs
                            links.append((full, depth + 1))

            with self.lock:
                self.pages_scanned += 1
            return {'url': url, 'emails': found, 'links': links, 'skipped': False}

        stream = url_subject.pipe(
            ops.map(lambda item: rx.from_callable(lambda: process(item)).pipe(ops.subscribe_on(scheduler))),
            ops.merge_all()
        )

        def on_next(res):
            if res.get('skipped'):
                return
            for e in res.get('emails', set()):
                with self.lock:
                    if e not in self.emails_found:
                        self.emails_found[e] = res['url']
                        print(f"    [!] Found: {e}")
            # enqueue discovered links
            for link in res.get('links', []):
                url_subject.on_next(link)
            with self.lock:
                if self.pages_scanned >= self.max_pages:
                    url_subject.on_completed()

        def on_error(err):
            print('[ERROR]', err)

        def on_completed():
            done_event.set()
            self.save_report(self.pages_scanned)

        subscription = stream.subscribe(on_next=on_next, on_error=on_error, on_completed=on_completed)

        # seed initial URL
        url_subject.on_next((self.start_url, 0))

        # wait for completion
        done_event.wait()

    def save_report(self, pages_scanned):
        print("\n" + "="*40)
        print("AUDIT COMPLETE")
        print("="*40)
        print(f"Pages scanned: {pages_scanned}")
        print(f"Unique emails found: {len(self.emails_found)}")
        print("-" * 20)

        # Clean emails before persisting
        cleaned_map = self._clean_emails_map(self.emails_found)
        self.emails_found = cleaned_map

        if self.emails_found:
            filename = 'audit_report.csv'
            try:
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Email Address', 'Source URL'])
                    for email, source in self.emails_found.items():
                        writer.writerow([email, source])
                print(f"[+] Detailed report saved to {filename}")
            except IOError as e:
                print(f"[-] Error saving file: {e}")

            # Save a simple top-emails list (one per line)
            try:
                with open('top_emails.txt', 'w', encoding='utf-8') as t:
                    for email in self.emails_found.keys():
                        t.write(email + '\n')
                print("[+] Top emails saved to top_emails.txt")
            except IOError:
                pass
        else:
            print("[-] No emails found.")

    def _clean_emails_map(self, raw_map):
        """Return a cleaned dict mapping cleaned_email -> source (first seen).
        Handles percent-encoding, HTML entities, common obfuscations and stray chars.
        """
        import html
        from urllib.parse import unquote

        def _clean_str(s):
            if not s or not isinstance(s, str):
                return ''
            s = s.strip()
            # Remove surrounding punctuation
            s = s.strip('\"\'"()[]{}<>.,;:\\')
            # Percent-decode and unescape HTML entities
            try:
                s = unquote(s)
            except Exception:
                pass
            s = html.unescape(s)
            # Normalize common obfuscations only if no @ present
            if '@' not in s:
                s = re.sub(r'\[at\]|\(at\)|\s+at\s+', '@', s, flags=re.IGNORECASE)
            # Normalize dot obfuscations
            s = re.sub(r'\[dot\]|\(dot\)|\s+dot\s+', '.', s, flags=re.IGNORECASE)
            # Remove whitespace around @ and dots
            s = re.sub(r'\s*@\s*', '@', s)
            s = re.sub(r'\s*\.\s*', '.', s)
            s = s.strip("'\"\n\r\t ")
            s = s.lower()
            return s

        email_rx = re.compile(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', re.IGNORECASE)
        cleaned = {}
        for raw, src in raw_map.items():
            candidate = _clean_str(raw)
            # If the cleaned string contains an email-like substring, extract it
            m = email_rx.search(candidate)
            if m:
                email = m.group(0).lower()
                if email not in cleaned:
                    cleaned[email] = src
            else:
                # as a last resort try directly on original raw value
                m2 = email_rx.search(str(raw))
                if m2:
                    email = m2.group(0).lower()
                    if email not in cleaned:
                        cleaned[email] = src
        return cleaned

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Email scanner (reactive)')
    parser.add_argument('target', help='Target website URL (e.g., https://www.example.com)')
    parser.add_argument('--no-render', action='store_true', help='Disable JS rendering (use requests only)')
    parser.add_argument('--workers', type=int, default=8, help='Number of worker threads for reactive crawl')
    parser.add_argument('--wait', type=int, default=2, help='Seconds to wait after page load when rendering JS')
    parser.add_argument('--max-depth', type=int, default=2, help='Maximum crawl depth')
    parser.add_argument('--max-pages', type=int, default=20, help='Maximum pages to scan')
    parser.add_argument('--sequential', action='store_true', help='Run original sequential crawler instead of reactive')

    args = parser.parse_args()

    target = args.target.strip()

    # tolerate calls like: python test2.py target='https://statusneo.com/'
    if target.lower().startswith('target='):
        target = target.split('=', 1)[1].strip()
    # strip surrounding single/double quotes if present
    if (target.startswith("'") and target.endswith("'")) or (target.startswith('"') and target.endswith('"')):
        target = target[1:-1].strip()

    if not target:
        print('Please provide a valid URL.')
        sys.exit(1)

    if not target.startswith('http'):
        target = 'https://' + target

    scanner = EmailScanner(target,
                           max_depth=args.max_depth,
                           max_pages=args.max_pages,
                           render_js=not args.no_render,
                           render_wait=args.wait)

    if args.sequential:
        scanner.crawl()
    else:
        scanner.crawl_rx(max_workers=args.workers)