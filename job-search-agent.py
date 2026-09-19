#!/usr/bin/env python3
"""
job_search_agent.py
--------------------
A small "agentic" job-search tool with a Tkinter GUI.

Given a keyword (and optional location), it tries multiple PUBLIC data
sources in order and stops as soon as one returns usable results:

    1. SerpApi's Google Jobs API   (best quality -- needs your own API key)
    2. LinkedIn's logged-out public job search results page
       (no login, no credentials -- this is the same page Google indexes)
    3. Indeed public search results
    4. ZipRecruiter public search results

Results (job title, company, location, short description, URL, source)
are written to a CSV file, and shown live in the GUI as they come in.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It does not log into LinkedIn (or anywhere) with a username/password.
LinkedIn's Terms of Service prohibit automated login + scraping, they
actively detect and permanently ban accounts that do it, and courts have
upheld enforcement action against it. That risk lands on YOUR account,
so this tool only ever touches LinkedIn's logged-out, publicly indexed
job-search results -- the same content search engines see -- never the
authenticated site.

SETUP
-----
    pip install requests beautifulsoup4

    # optional, for the best/most reliable results:
    # get a free-tier key at https://serpapi.com and either:
    #   export SERPAPI_KEY=your_key_here
    # or paste it into the GUI's "SerpApi key (optional)" field.

RUN
---
    python job_search_agent.py
"""

import csv
import os
import queue
import random
import threading
import time
import tkinter as tk
from dataclasses import dataclass, asdict, fields
from tkinter import ttk, filedialog, messagebox
from urllib.parse import quote_plus
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 15
SLEEP_RANGE = (1.5, 3.0)
MAX_RETRIES = 3
BACKOFF_BASE = 3.0   # seconds; doubles each retry, plus jitter
BLOCK_STATUS_CODES = {403, 429, 503}


def fetch_with_backoff(url: str, log, source_name: str, params=None,
                        max_retries: int = MAX_RETRIES) -> Optional[requests.Response]:
    """GET a URL with exponential backoff + jitter. Retries on network errors
    and on status codes that typically mean 'you got rate-limited / blocked'
    (403, 429, 503). Gives up and returns None after max_retries attempts,
    so a blocked source can never hang the agent -- it just moves on."""
    delay = BACKOFF_BASE
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            log(f"{source_name}: network error on attempt {attempt}/{max_retries} ({e}).")
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code in BLOCK_STATUS_CODES:
                log(f"{source_name}: HTTP {resp.status_code} (likely blocked/rate-limited), "
                    f"attempt {attempt}/{max_retries}.")
            else:
                # Other non-200 (404, 500, etc.) -- not a "blocked" signal,
                # no point retrying the same URL.
                log(f"{source_name}: HTTP {resp.status_code}, not retrying.")
                return resp

        if attempt < max_retries:
            wait = delay + random.uniform(0, 1.5)
            log(f"{source_name}: backing off {wait:.1f}s before retry...")
            time.sleep(wait)
            delay *= 2

    log(f"{source_name}: giving up after {max_retries} attempts.")
    return None


@dataclass
class JobResult:
    source: str
    title: str
    company: str
    location: str
    description: str
    url: str


def polite_sleep():
    time.sleep(random.uniform(*SLEEP_RANGE))


# ----------------------------------------------------------------------
# Source 1: SerpApi Google Jobs (needs an API key; most reliable/legal)
# ----------------------------------------------------------------------
def search_serpapi(keyword: str, location: str, api_key: str, log) -> List[JobResult]:
    if not api_key:
        log("SerpApi: no API key provided, skipping.")
        return []
    log("SerpApi: searching Google Jobs...")
    params = {
        "engine": "google_jobs",
        "q": keyword,
        "location": location or "United States",
        "api_key": api_key,
    }
    resp = fetch_with_backoff("https://serpapi.com/search", log, "SerpApi", params=params)
    if resp is None:
        return []
    try:
        data = resp.json()
    except Exception as e:
        log(f"SerpApi: could not parse response ({e})")
        return []

    if "error" in data:
        log(f"SerpApi: {data['error']}")
        return []

    results = []
    for job in data.get("jobs_results", []):
        results.append(JobResult(
            source="SerpApi/GoogleJobs",
            title=job.get("title", ""),
            company=job.get("company_name", ""),
            location=job.get("location", ""),
            description=(job.get("description") or "")[:400],
            url=(job.get("related_links") or [{}])[0].get("link", "") or job.get("job_id", ""),
        ))
    log(f"SerpApi: {len(results)} result(s).")
    return results


# ----------------------------------------------------------------------
# Source 2: LinkedIn's LOGGED-OUT public job search page (no credentials)
# ----------------------------------------------------------------------
def search_linkedin_public(keyword: str, location: str, log) -> List[JobResult]:
    log("LinkedIn (public, logged-out): searching...")
    q = quote_plus(keyword)
    loc = quote_plus(location or "United States")
    url = f"https://www.linkedin.com/jobs/search?keywords={q}&location={loc}"
    resp = fetch_with_backoff(url, log, "LinkedIn")
    if resp is None or resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    cards = soup.select("div.base-card") or soup.select("li")
    for card in cards:
        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        loc_el = card.select_one("span.job-search-card__location")
        link_el = card.select_one("a.base-card__full-link")
        if not title_el or not link_el:
            continue
        results.append(JobResult(
            source="LinkedIn (public)",
            title=title_el.get_text(strip=True),
            company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else "",
            description="",  # full description sits behind login; not fetched
            url=link_el.get("href", "").split("?")[0],
        ))
    log(f"LinkedIn (public): {len(results)} result(s).")
    return results


# ----------------------------------------------------------------------
# Source 3: Indeed public search
# ----------------------------------------------------------------------
def search_indeed(keyword: str, location: str, log) -> List[JobResult]:
    log("Indeed: searching...")
    q = quote_plus(keyword)
    loc = quote_plus(location or "")
    url = f"https://www.indeed.com/jobs?q={q}&l={loc}"
    resp = fetch_with_backoff(url, log, "Indeed")
    if resp is None or resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    cards = soup.select(".job_seen_beacon, .jobsearch-SerpJobCard")
    for card in cards:
        title_el = card.select_one("h2 a, .jobTitle a")
        company_el = card.select_one(".companyName")
        loc_el = card.select_one(".companyLocation")
        snippet_el = card.select_one(".job-snippet")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href.startswith("/"):
            href = "https://www.indeed.com" + href
        results.append(JobResult(
            source="Indeed",
            title=title_el.get_text(strip=True),
            company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else "",
            description=snippet_el.get_text(" ", strip=True) if snippet_el else "",
            url=href,
        ))
    log(f"Indeed: {len(results)} result(s).")
    return results


# ----------------------------------------------------------------------
# Source 4: ZipRecruiter public search
# ----------------------------------------------------------------------
def search_ziprecruiter(keyword: str, location: str, log) -> List[JobResult]:
    log("ZipRecruiter: searching...")
    q = quote_plus(f"{keyword} {location}".strip())
    url = f"https://www.ziprecruiter.com/candidate/search?search={q}"
    resp = fetch_with_backoff(url, log, "ZipRecruiter")
    if resp is None or resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    cards = soup.select("article") or soup.select("[class*=job_result]")
    for card in cards:
        title_el = card.select_one("h2 a, a[class*=job_link], a[data-job-id]")
        company_el = card.select_one("[class*=company]")
        loc_el = card.select_one("[class*=location]")
        if not title_el:
            continue
        results.append(JobResult(
            source="ZipRecruiter",
            title=title_el.get_text(strip=True),
            company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else "",
            description=card.get_text(" ", strip=True)[:300],
            url=title_el.get("href", ""),
        ))
    log(f"ZipRecruiter: {len(results)} result(s).")
    return results


# ----------------------------------------------------------------------
# Agent: tries each source in order, stops once it has enough results
# ----------------------------------------------------------------------
SOURCES = [
    ("SerpApi (Google Jobs)", search_serpapi),
    ("LinkedIn (public)", search_linkedin_public),
    ("Indeed", search_indeed),
    ("ZipRecruiter", search_ziprecruiter),
]


def run_agent(keyword: str, location: str, api_key: str, min_results: int,
              log, on_result) -> List[JobResult]:
    all_results: List[JobResult] = []
    for name, fn in SOURCES:
        if len(all_results) >= min_results:
            log(f"Have {len(all_results)} results already -- stopping before {name}.")
            break
        try:
            if fn is search_serpapi:
                batch = fn(keyword, location, api_key, log)
            else:
                batch = fn(keyword, location, log)
        except Exception as e:
            log(f"{name}: unexpected error ({e}) -- moving to next source.")
            batch = []
        for r in batch:
            all_results.append(r)
            on_result(r)
        if not batch:
            # this source likely got blocked/rate-limited (fetch_with_backoff
            # already retried it) -- give the next source a slightly longer
            # gap before hitting it, rather than hammering straight through.
            time.sleep(random.uniform(3.0, 5.0))
        else:
            polite_sleep()
    return all_results


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Job Search Agent")
        self.geometry("980x600")
        self.result_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.results: List[JobResult] = []
        self.worker_thread: Optional[threading.Thread] = None

        self._build_widgets()
        self.after(150, self._poll_queues)

    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Keyword:").grid(row=0, column=0, sticky="w")
        self.keyword_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.keyword_var, width=35).grid(row=0, column=1, **pad)

        ttk.Label(top, text="Location:").grid(row=0, column=2, sticky="w")
        self.location_var = tk.StringVar(value="United States")
        ttk.Entry(top, textvariable=self.location_var, width=25).grid(row=0, column=3, **pad)

        ttk.Label(top, text="SerpApi key (optional):").grid(row=1, column=0, sticky="w")
        self.apikey_var = tk.StringVar(value=os.environ.get("SERPAPI_KEY", ""))
        ttk.Entry(top, textvariable=self.apikey_var, width=35, show="*").grid(row=1, column=1, **pad)

        ttk.Label(top, text="Min. results before stopping:").grid(row=1, column=2, sticky="w")
        self.min_results_var = tk.IntVar(value=25)
        ttk.Spinbox(top, from_=5, to=200, textvariable=self.min_results_var, width=6).grid(
            row=1, column=3, sticky="w", **pad)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btn_frame, text="Search", command=self.start_search)
        self.start_btn.pack(side="left", padx=6)
        self.export_btn = ttk.Button(btn_frame, text="Export CSV...", command=self.export_csv,
                                      state="disabled")
        self.export_btn.pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(btn_frame, textvariable=self.status_var).pack(side="left", padx=12)

        # results table
        columns = [f.name for f in fields(JobResult)]
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        for col in columns:
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=140, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)

        # log box
        ttk.Label(self, text="Log:").pack(anchor="w", padx=6)
        self.log_box = tk.Text(self, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="x", padx=6, pady=(0, 6))

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_queues(self):
        while not self.result_queue.empty():
            r = self.result_queue.get_nowait()
            self.results.append(r)
            self.tree.insert("", "end", values=[getattr(r, f.name) for f in fields(JobResult)])
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(150, self._poll_queues)

    def start_search(self):
        keyword = self.keyword_var.get().strip()
        if not keyword:
            messagebox.showwarning("Missing keyword", "Enter a keyword to search for.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Busy", "A search is already running.")
            return

        self.tree.delete(*self.tree.get_children())
        self.results = []
        self.export_btn.configure(state="disabled")
        self.status_var.set("Searching...")
        self.start_btn.configure(state="disabled")

        location = self.location_var.get().strip()
        api_key = self.apikey_var.get().strip()
        min_results = self.min_results_var.get()

        def worker():
            run_agent(
                keyword, location, api_key, min_results,
                log=self.log,
                on_result=lambda r: self.result_queue.put(r),
            )
            self.log(f"Done. Total results: {len(self.results) + self.result_queue.qsize()}")
            self.status_var.set("Done.")
            self.start_btn.configure(state="normal")
            self.export_btn.configure(state="normal")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def export_csv(self):
        if not self.results:
            messagebox.showinfo("Nothing to export", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="job_search_results.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[f.name for f in fields(JobResult)])
            writer.writeheader()
            for r in self.results:
                writer.writerow(asdict(r))
        messagebox.showinfo("Exported", f"Saved {len(self.results)} rows to:\n{path}")


if __name__ == "__main__":
    App().mainloop()