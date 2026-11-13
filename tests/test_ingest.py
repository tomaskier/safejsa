import json
from pathlib import Path

import pytest

from safejsa import ingest
from safejsa.ingest import (
    LISTING_URL,
    CrawlBlocked,
    ListingRow,
    clean_text,
    find_pdf_url,
    load_tag_map,
    map_tags,
    parse_listing,
    select_alerts,
    split_text,
)
from safejsa.models import Alert

ROOT = Path(__file__).resolve().parents[1]


def _row(number, title, url, date="", tags=""):
    time = f'<time datetime="{date}T10:00:00Z">x</time>' if date else ""
    return f"""<tr>
      <td headers="view-field-safety-alert-number-table-column">{number} </td>
      <td headers="view-title-table-column"><a href="{url}" hreflang="en">{title}</a> </td>
      <td headers="view-field-document-posted-date-table-column">{time} </td>
      <td headers="view-field-safety-category-table-column">{tags} </td>
    </tr>"""


def _listing(*rows, last_page=0):
    pager = "".join(f'<a href="?page={page}">{page}</a>' for page in range(last_page + 1))
    return f"""<html><body><table><thead><tr><th>Number</th><th>Title</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table><nav>{pager}</nav></body></html>"""


def _listing_row(number, title, url="/safety-alerts/x", date=None, tags=()):
    return ListingRow(number, title, f"https://www.bsee.gov{url}", date, list(tags))


# --- Listing parsing ---------------------------------------------------------------------


def test_parse_listing_reads_rows_and_pager():
    html = _listing(
        _row(
            526,
            "MMA Safety Alert 526_Prevent Battery Ruptures",
            "/safety-alerts/526",
            "2026-10-30",
            "Batteries, Electrical",
        ),
        _row(2, "Safety Alert 2 - Shallow Drilling Hazards", "/safety-alerts/old-2"),
        last_page=12,
    )
    rows, last_page = parse_listing(html)
    assert last_page == 12
    assert rows[0] == ListingRow(
        526,
        "MMA Safety Alert 526_Prevent Battery Ruptures",
        "https://www.bsee.gov/safety-alerts/526",
        "2026-10-30",
        ["Batteries", "Electrical"],
    )
    assert rows[1].date is None and rows[1].tags == []


def test_parse_listing_unescapes_titles_and_ignores_header_row():
    rows, _ = parse_listing(_listing(_row(7, "Fire &amp; Explosion", "/a")))
    assert [row.title for row in rows] == ["Fire & Explosion"]


# --- Alert selection ---------------------------------------------------------------------


def test_select_skips_bulletins_joint_alerts_and_notices():
    rows = [
        _listing_row(16, "Safety Bulletin 016 - Operator Fails", "/safety-bulletins/016"),
        _listing_row(
            2, "Safety Alert 2 - Deepwater Horizon", "/safety-alerts/joint-safety-alert-2"
        ),
        _listing_row(483, "Scam Alert-Suspicious Requests for Payment", "/safety-alerts/scam"),
        _listing_row(347, "Safety Alert 347 - Dropped Riser Joint Nearly Crushes Floorhand"),
    ]
    kept, skipped = select_alerts(rows)
    assert [row.number for row in kept] == [347]
    assert {item.number for item in skipped} == {16, 2, 483}


def test_select_merges_repeats_and_prefers_the_dated_row():
    undated = _listing_row(231, "Safety Alert 231 - Human Engineering Factors", "/a")
    dated = _listing_row(231, "Safety Alert 231 - Human Engineering Factors", "/b", "2005-05-31")
    kept, skipped = select_alerts([undated, dated])
    assert kept == [dated]
    assert [item.reason for item in skipped] == ["duplicate listing of the same alert"]


def test_select_skips_numbers_shared_by_different_alerts():
    rows = [
        _listing_row(42, "Safety Alert 42 - Hydraulic Pump Reservoir Ruptures", "/a"),
        _listing_row(42, "Safety Alert 42 - Arguello Inc. Platforms Harvest", "/b"),
        _listing_row(43, "Safety Alert 43 - Shallow Gas Blowout", "/c"),
    ]
    kept, skipped = select_alerts(rows)
    assert [row.number for row in kept] == [43]
    assert len(skipped) == 2
    assert all("different alerts" in item.reason for item in skipped)


# --- Alert text --------------------------------------------------------------------------

MODERN = """SAFETY ALERT
Contact: bseepublicaffairs@bsee.gov
Phone: 800.200.4853
Fingerboard Locking Bolt Failure
Six stands of pipe fell from the finger row.
Safety Alert No. 480
A Safety Alert is a tool used by BSEE to inform the offshore oil and gas industry of the
circumstances surrounding a potential safety issue. It also contains recommendations
that could assist avoiding potential incidents on the Outer Continental Shelf.
BSEE recommends that operators consider the following:
 Inspecting all fingerboard locking bolts.
– BSEE –"""


def test_clean_text_drops_headers_contacts_and_boilerplate():
    text = clean_text(MODERN)
    assert "Contact" not in text and "800.200" not in text
    assert "Safety Alert No. 480" not in text
    assert "A Safety Alert is a tool" not in text
    assert "– BSEE –" not in text
    assert "• Inspecting all fingerboard locking bolts." in text


def test_split_text_at_the_recommendations():
    summary, recommendations = split_text(clean_text(MODERN))
    assert summary.endswith("Six stands of pipe fell from the finger row.")
    assert recommendations.startswith("BSEE recommends that operators")


def test_split_text_old_alert_and_no_recommendations():
    old = "A fire occurred on a platform.\nTo prevent a recurrence, the operator will bleed down."
    assert split_text(old) == (
        "A fire occurred on a platform.",
        "To prevent a recurrence, the operator will bleed down.",
    )
    assert split_text("Only a narrative.\nThe manufacturer's recommended torque.") == (
        "Only a narrative.\nThe manufacturer's recommended torque.",
        "",
    )


def test_find_pdf_url_makes_absolute_links():
    page = '<a href="/sites/bsee.gov/files/2026-10/Alert%20526.pdf">PDF</a>'
    assert find_pdf_url(page, "https://www.bsee.gov/safety-alerts/526") == (
        "https://www.bsee.gov/sites/bsee.gov/files/2026-10/Alert%20526.pdf"
    )
    assert find_pdf_url("<p>no links</p>", "https://www.bsee.gov/x") is None


# --- Tags --------------------------------------------------------------------------------


def test_map_tags_with_the_real_taxonomy():
    tag_map = load_tag_map(ROOT / "data" / "taxonomy.yaml")
    assert map_tags(["Cranes/Lifting"], tag_map) == (["lifting"], [])
    assert map_tags(["batteries", "Electrical"], tag_map) == (["electrical"], [])
    assert map_tags(["BOP/LMRP Failures"], tag_map) == (
        ["equipment_failure", "well_operations"],
        [],
    )
    assert map_tags(["Personnel Safety", "Fire"], tag_map) == (
        ["hot_work_fire"],
        ["Personnel Safety"],
    )
    assert map_tags([], tag_map) == (["other"], [])


# --- End to end with a fake fetcher ------------------------------------------------------


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, *, cache=True):
        self.requested.append(url)
        if url not in self.pages:
            raise ValueError(f"HTTP 404 for {url}")
        return self.pages[url]


def _site(robots="User-agent: *\nDisallow: /admin/\n"):
    base = "https://www.bsee.gov"
    return {
        f"{base}/robots.txt": robots.encode(),
        LISTING_URL: _listing(
            _row(
                480,
                "BSEE Safety Alert 480 - Fingerboard",
                "/safety-alerts/480",
                "2024-01-25",
                "Pipe Handling, Personnel Safety",
            ),
            last_page=1,
        ).encode(),
        f"{LISTING_URL}?page=1": _listing(
            _row(150, "Safety Alert 150 - Fire During Cutting", "/safety-alerts/150"),
            _row(149, "Safety Alert 149 - Missing PDF", "/safety-alerts/149"),
            _row(16, "Safety Bulletin 016 - Contractor", "/safety-bulletins/016"),
            last_page=1,
        ).encode(),
        f"{base}/safety-alerts/480": b'<a href="/files/480.pdf">PDF</a>',
        f"{base}/safety-alerts/150": b'<a href="/files/150.pdf">PDF</a>',
        f"{base}/safety-alerts/149": b"<p>No attachment</p>",
        f"{base}/files/480.pdf": b"pdf-480",
        f"{base}/files/150.pdf": b"pdf-150",
    }


TEXTS = {
    b"pdf-480": clean_text(MODERN),
    b"pdf-150": "A fire occurred while casing was cut and slag fell on a sump tank. " * 3,
}


def test_run_ingest_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "taxonomy.yaml").write_text(
        (ROOT / "data" / "taxonomy.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(ingest, "extract_text", TEXTS.__getitem__)

    report = ingest.run_ingest(FakeFetcher(_site()))

    lines = (tmp_path / "data" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    alerts = [Alert.model_validate(json.loads(line)) for line in lines]
    assert [alert.alert_number for alert in alerts] == [150, 480]
    assert alerts[1].categories == ["well_operations"]
    assert alerts[1].recommendations.startswith("BSEE recommends")
    assert alerts[1].pdf_url == "https://www.bsee.gov/files/480.pdf"
    assert alerts[0].categories == ["other"] and alerts[0].recommendations == ""
    assert [item.number for item in report.failed] == [149]

    out = capsys.readouterr().out
    assert "Wrote 2 alerts" in out
    assert "#149: Safety Alert 149 - Missing PDF: no PDF linked" in out
    assert "Personnel Safety" in out
    assert "#16: Safety Bulletin 016" in out


def test_robots_disallow_stops_the_crawl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fetcher = FakeFetcher(_site(robots="User-agent: *\nDisallow: /guidance-and-regulations/\n"))
    with pytest.raises(CrawlBlocked):
        ingest.crawl(fetcher, {})
    assert len(fetcher.requested) == 1
