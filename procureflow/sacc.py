"""Harvest the SACC Manual (Standard Acquisition Clauses and Conditions) from CanadaBuys.

Run on a machine that can reach canadabuys.canada.ca:
    python -m procureflow sync-clauses [--out sacc_clauses.csv]

It crawls the manual starting at START_URL, stays on canadabuys.canada.ca within the SACC
section, and imports every page whose heading looks like a clause: "<code> (<yyyy-mm-dd>) <title>",
e.g. "2035 (2022-12-01) General Conditions - Higher Complexity - Services".
Only the newest version of each code is kept. Re-run it whenever PSPC publishes updates.
"""
import csv
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

START_URL = ("https://canadabuys.canada.ca/en/how-procurement-works/policies-and-guidelines/"
             "standard-acquisition-clauses-and-conditions-sacc-manual")
HOST = "canadabuys.canada.ca"
SECTION_HINTS = ("standard-acquisition-clauses", "sacc")
CLAUSE_HEADING = re.compile(r"^\s*(?:SACC\s+)?([A-Z]{0,2}\d{4}[A-Z]{0,2})\s*\((\d{4}-\d{2}-\d{2})\)\s*[-–:]?\s*(.+?)\s*$")
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript"}
BLOCK_TAGS = {"p", "li", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.headings, self.parts = [], [], []
        self._skip = 0
        self._in_main = 0
        self._heading = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag == "main" or a.get("role") == "main":
            self._in_main += 1
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in ("h1", "h2"):
            self._heading = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "main" and self._in_main:
            self._in_main -= 1
        if tag in ("h1", "h2") and self._heading is not None:
            self.headings.append(" ".join("".join(self._heading).split()))
            self._heading = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)
        if not self._skip:
            self.parts.append(data)

    def text(self):
        lines = [" ".join(l.split()) for l in "".join(self.parts).split("\n")]
        out, blank = [], False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ProcureFlow-SACC-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def _in_scope(url):
    p = urllib.parse.urlparse(url)
    return p.scheme in ("http", "https") and p.netloc == HOST and \
        any(h in p.path.lower() for h in SECTION_HINTS) and not p.path.lower().endswith((".pdf", ".doc", ".docx"))


def parse_clause(url, html):
    page = _Page()
    page.feed(html)
    for h in page.headings:
        m = CLAUSE_HEADING.match(h)
        if m:
            code, version, title = m.groups()
            body = page.text()
            start = body.find(h.split("(")[0].strip())
            return {"code": code, "version_date": version, "title": title,
                    "text": body[start:] if start >= 0 else body, "source_url": url}, page.links
    return None, page.links


def harvest(start_url=START_URL, fetch=_fetch, max_pages=5000, delay=0.5, log=print):
    seen, queue, clauses = set(), [start_url], {}
    while queue and len(seen) < max_pages:
        url = urllib.parse.urldefrag(queue.pop(0))[0]
        if url in seen:
            continue
        seen.add(url)
        try:
            html = fetch(url)
        except Exception as exc:  # keep crawling; report at the end
            log(f"skip {url}: {exc}")
            continue
        clause, links = parse_clause(url, html)
        if clause:
            old = clauses.get(clause["code"])
            if not old or clause["version_date"] >= old["version_date"]:
                clauses[clause["code"]] = clause
        for href in links:
            nxt = urllib.parse.urldefrag(urllib.parse.urljoin(url, href))[0]
            if nxt not in seen and _in_scope(nxt):
                queue.append(nxt)
        if len(seen) % 50 == 0:
            log(f"{len(seen)} pages read, {len(clauses)} clauses found")
        if delay:
            time.sleep(delay)
    return sorted(clauses.values(), key=lambda c: c["code"])


def write_csv(path, clauses):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "title", "text", "version_date", "source_url"])
        w.writeheader()
        w.writerows(clauses)
