"""Crawl the BSEE Safety Alerts listing, extract each alert PDF and write data/alerts.jsonl.

The crawl is polite: it checks robots.txt, sends one request at a time with a pause between
them, and caches alert pages and PDFs in data/cache/ so a re-run downloads only new alerts.
Listing pages are always fetched fresh, so new alerts are found.

Only numbered Safety Alerts are kept, so the alert number stays a unique key (SPEC.md, Data).
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import yaml
from pypdf import PdfReader

from safejsa.models import Alert

BASE_URL = "https://www.bsee.gov"
LISTING_URL = f"{BASE_URL}/guidance-and-regulations/guidance/safety-alerts-program"
USER_AGENT = "SafeJSA/0.1 (research crawler; +https://github.com/tomaskier/safejsa)"
DELAY_SECONDS = 1.0
MIN_TEXT_CHARS = 100

ALERTS_PATH = Path("data/alerts.jsonl")
CACHE_DIR = Path("data/cache")
TAXONOMY_PATH = Path("data/taxonomy.yaml")


class CrawlBlocked(RuntimeError):
    """BSEE refused the crawl (robots.txt, 401/403 or 429). Stop and ask the user."""


# --- Fetching ----------------------------------------------------------------------------


class Fetch(Protocol):
    def get(self, url: str, *, cache: bool = True) -> bytes: ...


class Fetcher:
    """Downloads one URL at a time, at most one request per `delay` seconds, with a cache."""

    def __init__(self, cache_dir: Path = CACHE_DIR, delay: float = DELAY_SECONDS) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self._last_request = 0.0

    def get(self, url: str, *, cache: bool = True) -> bytes:
        path = self._cache_path(url)
        if cache and path.exists():
            return path.read_bytes()
        data = self._download(url)
        if cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def _cache_path(self, url: str) -> Path:
        suffix = ".pdf" if urlparse(url).path.lower().endswith(".pdf") else ".html"
        return self.cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + suffix)

    def _download(self, url: str) -> bytes:
        error: Exception | None = None
        for attempt in range(3):
            wait = self._last_request + self.delay * (2**attempt) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    raise CrawlBlocked(f"BSEE answered HTTP {exc.code} for {url}") from exc
                if exc.code < 500:
                    raise
                error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                error = exc
        assert error is not None
        raise error


# --- Listing pages -----------------------------------------------------------------------


@dataclass
class ListingRow:
    number: int | None
    title: str
    url: str
    date: str | None
    tags: list[str]


class _ListingParser(HTMLParser):
    """Reads the rows of the listing table (Number, Title, Date, Category) and the pager."""

    COLUMNS = {
        "alert-number": "number",
        "title": "title",
        "posted-date": "date",
        "safety-category": "tags",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, dict]] = []
        self.last_page = 0
        self._row: dict[str, dict] | None = None
        self._column: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        href = attributes.get("href") or ""
        if tag == "a":
            page = re.search(r"[?&]page=(\d+)", href)
            if page:
                self.last_page = max(self.last_page, int(page.group(1)))
        if tag == "tr":
            self._row = {}
        elif tag == "td" and self._row is not None:
            headers = attributes.get("headers") or ""
            self._column = next(
                (name for key, name in self.COLUMNS.items() if key in headers), None
            )
            if self._column:
                self._row[self._column] = {"text": [], "href": None, "datetime": None}
        elif self._row is not None and self._column:
            cell = self._row[self._column]
            if tag == "a" and cell["href"] is None:
                cell["href"] = href
            elif tag == "time":
                cell["datetime"] = attributes.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._column = None
        elif tag == "tr" and self._row is not None:
            if self._row.get("title", {}).get("href"):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._column:
            self._row[self._column]["text"].append(data)


def parse_listing(html_text: str, base_url: str = BASE_URL) -> tuple[list[ListingRow], int]:
    """Return the table rows of one listing page and the highest page number in its pager."""
    parser = _ListingParser()
    parser.feed(html_text)
    rows = []
    for raw in parser.rows:

        def text(column: str, raw: dict = raw) -> str:
            cell = raw.get(column)
            return " ".join("".join(cell["text"]).split()) if cell else ""

        number = text("number")
        stamp = (raw.get("date") or {}).get("datetime")
        rows.append(
            ListingRow(
                number=int(number) if number.isdigit() else None,
                title=text("title"),
                url=urljoin(base_url, raw["title"]["href"]),
                date=stamp[:10] if stamp else None,
                tags=[tag.strip() for tag in text("tags").split(",") if tag.strip()],
            )
        )
    return rows, parser.last_page


# --- Choosing which entries are Safety Alerts --------------------------------------------

_NOT_ALERT = re.compile(r"safety[-\s]bulletin|joint[-\s]safety[-\s]alert|scam[-\s]alert", re.I)


@dataclass
class Skipped:
    number: int | None
    title: str
    url: str
    reason: str


def _same_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def select_alerts(rows: list[ListingRow]) -> tuple[list[ListingRow], list[Skipped]]:
    """Keep one row per alert number; skip non-alerts and numbers shared by different alerts.

    Rows that repeat the same number and title are merged, keeping a dated row if one exists.
    A number that names more than one different alert is skipped entirely, because the
    alert number must stay a unique key.
    """
    skipped: list[Skipped] = []
    by_number: dict[int, list[ListingRow]] = defaultdict(list)
    for row in rows:
        if row.number is None:
            skipped.append(Skipped(None, row.title, row.url, "no alert number"))
        elif _NOT_ALERT.search(row.title) or _NOT_ALERT.search(row.url):
            skipped.append(
                Skipped(
                    row.number, row.title, row.url, "not a Safety Alert (bulletin, joint or notice)"
                )
            )
        else:
            by_number[row.number].append(row)

    kept: list[ListingRow] = []
    for number in sorted(by_number):
        group = by_number[number]
        if len({_same_title(row.title) for row in group}) > 1:
            reason = f"number {number} names {len(group)} different alerts"
            skipped += [Skipped(number, row.title, row.url, reason) for row in group]
            continue
        keep = next((row for row in group if row.date), group[0])
        kept.append(keep)
        skipped += [
            Skipped(number, row.title, row.url, "duplicate listing of the same alert")
            for row in group
            if row is not keep
        ]
    return kept, skipped


# --- Alert text --------------------------------------------------------------------------

_PDF_LINK = re.compile(r"""href=["']([^"']+?\.pdf)(?:[?#][^"']*)?["']""", re.I)
_BULLETS = str.maketrans({char: "•" for char in "▪●‣�"})
_NOISE_LINE = re.compile(
    r"^(?:safety alert(?: no\.?)?\s*\d*(?:\s+-\s.*)?|category:.*|[-–—\s]*bsee[-–—\s]*|\d+ of \d+\b.*\d{1,2}:\d{2}.*|.*https?://\S*\.\.\..*|https?://\S*)$",
    re.I,
)
_CONTACT = re.compile(r"\b(?:contact|phone|e-?mail)\s*:.*$", re.I)
_BOILERPLATE = re.compile(
    r"A Safety Alert is a tool used by .*?(?:Outer\s+Continental\s+Shelf|OCS)\s*\.", re.I | re.S
)
_RECOMMENDS = re.compile(
    r"\brecommend(?:s|ed)?\s+(?:that|the\s+following|operators|to)\b"
    r"|\bfollowing\s+recommendations\b"
    r"|\bto\s+prevent\s+(?:a\s+)?(?:recurrence|similar)",
    re.I,
)


def find_pdf_url(page_html: str, page_url: str) -> str | None:
    """Return the first PDF linked from an alert page, as an absolute URL."""
    match = _PDF_LINK.search(page_html)
    return urljoin(page_url, match.group(1)) if match else None


def extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return clean_text("\n".join(page.extract_text() or "" for page in reader.pages))


def clean_text(text: str) -> str:
    """Normalise PDF text and drop page headers, contact lines and the standard boilerplate."""
    text = unicodedata.normalize("NFKC", text).translate(_BULLETS)
    text = _BOILERPLATE.sub("", text)
    lines = []
    for line in text.splitlines():
        line = _CONTACT.sub("", " ".join(line.split())).strip()
        if line and not _NOISE_LINE.match(line):
            lines.append(line)
    return "\n".join(lines)


def split_text(text: str) -> tuple[str, str]:
    """Split alert text into (summary, recommendations) at the line that starts them."""
    match = _RECOMMENDS.search(text)
    if not match:
        return text.strip(), ""
    start = text.rfind("\n", 0, match.start()) + 1
    return text[:start].strip(), text[start:].strip()


# --- Tags --------------------------------------------------------------------------------


def load_tag_map(path: Path = TAXONOMY_PATH) -> dict[str, list[str]]:
    """Map each lower-cased BSEE tag to the taxonomy IDs it belongs to."""
    taxonomy = yaml.safe_load(path.read_text(encoding="utf-8"))
    tag_map: dict[str, list[str]] = defaultdict(list)
    for category_id, entry in taxonomy["categories"].items():
        for tag in entry["bsee_tags"]:
            tag_map[tag.lower()].append(category_id)
    return dict(tag_map)


def map_tags(tags: list[str], tag_map: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Return (taxonomy IDs, unmapped tags). IDs fall back to ["other"]."""
    ids: list[str] = []
    unmapped: list[str] = []
    for tag in tags:
        found = tag_map.get(tag.lower())
        if found is None:
            unmapped.append(tag)
        ids += [category for category in found or [] if category not in ids]
    return ids or ["other"], unmapped


# --- The ingest run ----------------------------------------------------------------------


@dataclass
class IngestReport:
    alerts: list[Alert] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    failed: list[Skipped] = field(default_factory=list)
    unmapped_tags: Counter = field(default_factory=Counter)


def build_alert(
    row: ListingRow, fetcher: Fetch, tag_map: dict[str, list[str]]
) -> tuple[Alert, list[str]]:
    """Download and parse one alert. Raises ValueError when it cannot be parsed."""
    if urlparse(row.url).path.lower().endswith(".pdf"):
        pdf_url = row.url
    else:
        page = fetcher.get(row.url).decode("utf-8", errors="replace")
        pdf_url = find_pdf_url(page, row.url)
        if pdf_url is None:
            raise ValueError("no PDF linked from the alert page")
    text = extract_text(fetcher.get(pdf_url))
    if len("".join(text.split())) < MIN_TEXT_CHARS:
        raise ValueError("no extractable text in the PDF (scanned image?)")
    summary, recommendations = split_text(text)
    categories, unmapped = map_tags(row.tags, tag_map)
    alert = Alert(
        alert_number=row.number,
        title=row.title,
        date=row.date,
        bsee_tags=row.tags,
        categories=categories,
        summary=summary,
        recommendations=recommendations,
        url=row.url,
        pdf_url=pdf_url,
    )
    return alert, unmapped


def crawl(fetcher: Fetch, tag_map: dict[str, list[str]], echo=print) -> IngestReport:
    robots = urllib.robotparser.RobotFileParser()
    robots.parse(fetcher.get(f"{BASE_URL}/robots.txt", cache=False).decode("utf-8").splitlines())

    def allowed(url: str) -> str:
        if not robots.can_fetch(USER_AGENT, url):
            raise CrawlBlocked(f"robots.txt disallows {url}")
        return url

    first, last_page = parse_listing(fetcher.get(allowed(LISTING_URL), cache=False).decode("utf-8"))
    rows = list(first)
    for page in range(1, last_page + 1):
        url = allowed(f"{LISTING_URL}?page={page}")
        rows += parse_listing(fetcher.get(url, cache=False).decode("utf-8"))[0]
    if not rows:
        raise ValueError("found no alerts on the listing page; has the BSEE page layout changed?")
    echo(f"Listing: {len(rows)} entries on {last_page + 1} pages.")

    kept, skipped = select_alerts(rows)
    report = IngestReport(skipped=skipped)
    echo(f"Downloading {len(kept)} Safety Alerts (cached in {CACHE_DIR.as_posix()}/).")
    for index, row in enumerate(kept, start=1):
        try:
            allowed(row.url)
            alert, unmapped = build_alert(row, fetcher, tag_map)
        except CrawlBlocked:
            raise
        except Exception as exc:  # one bad alert must not stop the crawl
            report.failed.append(
                Skipped(row.number, row.title, row.url, str(exc) or type(exc).__name__)
            )
        else:
            report.alerts.append(alert)
            report.unmapped_tags.update(unmapped)
        if index % 25 == 0 or index == len(kept):
            echo(f"  {index}/{len(kept)}")
    return report


def write_alerts(alerts: list[Alert], path: Path = ALERTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for alert in sorted(alerts, key=lambda item: item.alert_number):
            handle.write(alert.model_dump_json() + "\n")
    temporary.replace(path)


def print_report(report: IngestReport, path: Path, echo=print) -> None:
    echo("")
    echo(f"Wrote {len(report.alerts)} alerts to {path.as_posix()}.")
    no_recommendations = sum(1 for alert in report.alerts if not alert.recommendations)
    echo(f"{no_recommendations} of them have no recommendations section.")

    echo(f"\nSkipped {len(report.skipped)} listing entries:")
    for reason, count in Counter(item.reason for item in report.skipped).most_common():
        echo(f"  {count:4d}  {reason}")
    for item in sorted(report.skipped, key=lambda item: (item.number or 0, item.title)):
        echo(f"    #{item.number}: {item.title} [{item.reason}]")

    echo(f"\nCould not parse {len(report.failed)} alerts:")
    for item in report.failed:
        echo(f"  #{item.number}: {item.title}: {item.reason} ({item.url})")

    echo(f"\nUnmapped BSEE tags (sent to `other`; map them in {TAXONOMY_PATH.as_posix()}):")
    for tag, count in report.unmapped_tags.most_common():
        echo(f"  {count:4d}  {tag}")


def run_ingest(fetcher: Fetch | None = None) -> IngestReport:
    """`safejsa ingest`: crawl BSEE, write data/alerts.jsonl and print what was left out."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # titles may hold characters the console lacks
    fetcher = fetcher or Fetcher()
    try:
        report = crawl(fetcher, load_tag_map())
    except CrawlBlocked as exc:
        print(f"Stopped: {exc}. Ask the user before crawling again.", file=sys.stderr)
        raise SystemExit(2) from None
    if not report.alerts:
        raise SystemExit("No alert could be parsed; data/alerts.jsonl was not changed.")
    write_alerts(report.alerts)
    print_report(report, ALERTS_PATH)
    return report
