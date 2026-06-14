"""parse_email_file body selection: prefer HTML, with a collapse guard.

multipart/alternative senders often strip structured fields (account numbers,
due dates, totals) from the text/plain copy — those survive only in the HTML.
parse_email_file therefore prefers the HTML-derived body, falling back to plain
text when HTML is absent or html_to_text collapses to under half its length.
"""

from email.message import EmailMessage
from pathlib import Path

from mail_semantic_search.mailmate_reader import (
    extract_html_text,
    parse_email_file,
)


def _write_eml(tmp_path: Path, *, plain: str | None, html: str | None) -> Path:
    msg = EmailMessage()
    msg["Subject"] = "New Electric Bill"
    msg["From"] = "noreply@notifications.eversource.com"
    msg["To"] = "jon@example.com"
    msg["Message-ID"] = "<test-body-selection@example.com>"
    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif plain is not None:
        msg.set_content(plain)
    elif html is not None:
        msg.set_content(html, subtype="html")
    path = tmp_path / "msg.eml"
    path.write_bytes(msg.as_bytes())
    return path


def test_prefers_html_when_it_carries_more(tmp_path):
    """HTML has account #/due date the plain-text copy omits — HTML wins."""
    plain = (
        "Your monthly electric bill is now available. "
        "Sign in to view your bill online."
    )
    html = """
    <html><body>
      <h1>Bill Ready</h1>
      <p>Account Number: ******7309</p>
      <p>Bill date: 06/03/2026</p>
      <p>Due date: 06/28/2026</p>
      <p>Total Amount Due: $627.25</p>
      <p>Your monthly electric bill is now available.
         Sign in to view your bill online.</p>
    </body></html>
    """
    result = parse_email_file(_write_eml(tmp_path, plain=plain, html=html))
    body = result["body"]
    assert "******7309" in body
    assert "06/28/2026" in body
    assert "$627.25" in body


def test_falls_back_to_plain_when_html_collapses(tmp_path):
    """An essentially-empty HTML part must not displace a real plain-text body."""
    plain = (
        "This is the full, substantive plain text body of the message. "
        "It contains all of the meaningful content a reader would expect "
        "to find, across several sentences of real prose."
    )
    # html_to_text yields under _MIN_HTML_BODY_CHARS here — a genuine collapse.
    html = "<html><body><p>.</p></body></html>"
    result = parse_email_file(_write_eml(tmp_path, plain=plain, html=html))
    assert result["body"].startswith("This is the full, substantive plain text")


def test_prefers_clean_html_even_when_shorter_than_plain(tmp_path):
    """The collapse guard is an absolute floor, NOT a ratio against plain.

    Regression: link-stripping legitimately shortens HTML. Clean HTML that is
    shorter than the (URL-bloated) plain-text copy must still win, rather than
    tripping a ratio-based "collapse" check and falling back to plain.
    """
    long_url = "https://track.example.com/c/" + "x" * 900
    # Plain text is long only because it carries a bare tracking URL.
    plain = f"Track my shipment: {long_url}"
    # Clean HTML is short (URL stripped) but carries the real, structured content.
    html = f"""
    <html><body>
      <p>Order US70373 is out for delivery. Estimated delivery: June 9.</p>
      <a href="{long_url}">Track my shipment</a>
    </body></html>
    """
    result = parse_email_file(_write_eml(tmp_path, plain=plain, html=html))
    body = result["body"]
    assert "Order US70373" in body          # HTML content chosen
    assert "track.example.com" not in body  # tracking URL stripped, not reintroduced
    assert len(body) < len(plain)           # clean HTML won despite being shorter


def test_uses_html_when_no_plain_part(tmp_path):
    html = "<html><body><p>HTML only content here.</p></body></html>"
    result = parse_email_file(_write_eml(tmp_path, plain=None, html=html))
    assert "HTML only content here." in result["body"]


def test_uses_plain_when_no_html_part(tmp_path):
    plain = "Plain text only content here."
    result = parse_email_file(_write_eml(tmp_path, plain=plain, html=None))
    assert "Plain text only content here." in result["body"]


def test_extract_html_text_returns_none_without_html_part(tmp_path):
    msg = EmailMessage()
    msg.set_content("just plain")
    assert extract_html_text(msg) is None
