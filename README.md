**Job Search Agent**
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
    python job-search-agent.py
