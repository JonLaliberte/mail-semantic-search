"""Unit tests for html_extract.html_to_text."""

from mail_semantic_search.html_extract import html_to_text


def test_strips_style_block_contents():
    html = """
    <html><head><style>.foo { color: red; padding: 10px; }</style></head>
    <body><p>Hello world</p></body></html>
    """
    out = html_to_text(html)
    assert "Hello world" in out
    assert "color:" not in out
    assert "padding" not in out
    assert "{" not in out


def test_strips_script_block_contents():
    html = """
    <html><body>
    <script>var tracker = 'pixel';</script>
    <p>Body text here</p>
    </body></html>
    """
    out = html_to_text(html)
    assert "Body text here" in out
    assert "tracker" not in out
    assert "var" not in out


def test_decodes_html_entities():
    html = "<p>Foo&nbsp;bar &amp; baz</p>"
    out = html_to_text(html)
    assert "&nbsp;" not in out
    assert "&amp;" not in out
    assert "Foo" in out
    assert "bar" in out
    assert "baz" in out


def test_drops_hidden_preheader():
    html = """
    <html><body>
    <div style="display:none">Hidden preheader text</div>
    <p>Visible content</p>
    </body></html>
    """
    out = html_to_text(html)
    assert "Hidden preheader text" not in out
    assert "Visible content" in out


def test_drops_preheader_by_class():
    html = """
    <html><body>
    <span class="preheader">Sneaky preheader</span>
    <p>Actual body</p>
    </body></html>
    """
    out = html_to_text(html)
    assert "Sneaky preheader" not in out
    assert "Actual body" in out


def test_replaces_img_with_alt():
    html = '<p>Logo: <img src="logo.png" alt="ACME Corp"></p>'
    out = html_to_text(html)
    assert "ACME Corp" in out
    assert "logo.png" not in out


def test_drops_img_without_alt():
    html = '<p>Before<img src="tracking.gif">After</p>'
    out = html_to_text(html)
    assert "Before" in out
    assert "After" in out
    assert "tracking.gif" not in out


def test_keeps_links_in_markdown_format():
    html = '<p>Visit <a href="https://example.com">our site</a> today</p>'
    out = html_to_text(html)
    assert "[our site](https://example.com)" in out


def test_collapses_redundant_link_text_url():
    html = '<p>See <a href="https://example.com/page">https://example.com/page</a></p>'
    out = html_to_text(html)
    assert "[https://example.com/page](https://example.com/page)" not in out
    assert "https://example.com/page" in out


def test_truncates_at_unsubscribe_footer():
    body = "This is the real content of the message. " * 20  # ~840 chars
    # Marker on its own line (after a <hr>) to satisfy line-anchored matcher.
    html = f"""
    <html><body>
    <p>{body}</p>
    <hr>
    <p>Unsubscribe from these emails.</p>
    <p>Some footer junk.</p>
    </body></html>
    """
    out = html_to_text(html)
    assert "real content" in out
    assert "unsubscribe" not in out.lower()
    assert "footer junk" not in out


def test_does_not_truncate_short_body_at_marker():
    """A short transactional email mentioning unsubscribe shouldn't be gutted."""
    html = "<p>Click unsubscribe to opt out.</p>"
    out = html_to_text(html)
    assert "Click" in out


def test_squeezes_blank_runs():
    html = "<p>Para 1</p><br><br><br><br><br><p>Para 2</p>"
    out = html_to_text(html)
    assert "\n\n\n" not in out


def test_empty_input_returns_empty():
    assert html_to_text("") == ""


def test_real_national_grid_email_keeps_bill_amount():
    """Regression for v1 footer-truncator bug.

    v1 matched 'view in browser' anywhere in the text and gutted any email
    that started with the common 'Having trouble viewing this email? Click
    here to view in browser.' preamble — dropping bill amounts, dates,
    everything after char ~316.

    A line-anchored matcher fixes this. The bill amount, due date, and
    'Pay Now' link must all survive.
    """
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "national_grid_marketing.html"
    html = fixture.read_text()
    out = html_to_text(html)

    assert "$255.40" in out, "bill amount was dropped by footer truncator"
    assert "May 21, 2026" in out, "due date was dropped"
    assert "Pay Now" in out, "Pay Now link was dropped"
    # The preamble "Click here to view in browser" used to trigger truncation
    # in v1 because the marker match wasn't line-anchored. The actual content
    # body must follow it.
    assert out.index("$255.40") > out.index("view in browser"), (
        "bill amount must appear AFTER the preamble that v1 used to truncate at"
    )


def test_line_anchored_marker_ignores_mid_sentence():
    """A marker word in the middle of a sentence must not trigger truncation."""
    body = "Real content paragraph. " * 30  # ~720 chars
    html = f"<html><body><p>{body}</p><p>Click here to unsubscribe please.</p></body></html>"
    out = html_to_text(html)
    # Both should survive because "unsubscribe" appears mid-line.
    assert "Real content" in out
    assert "Click here to unsubscribe" in out


def test_line_anchored_marker_fires_on_separated_footer():
    """A marker that begins its own line should trigger truncation."""
    body = "Important transactional content. " * 30  # ~990 chars
    html = (
        f"<html><body><p>{body}</p>"
        "<hr>"
        "<p>Unsubscribe | Manage preferences</p>"
        "</body></html>"
    )
    out = html_to_text(html)
    assert "Important transactional content" in out
    assert "unsubscribe" not in out.lower()
    assert "Manage preferences" not in out


def test_marketing_email_smoke_test():
    """The kind of CSS/style noise the old regex strip left behind."""
    html = """
    <html>
    <head>
      <style type="text/css">
        body { margin: 0; padding: 0; }
        .container { width: 600px; background-color: #ffffff; }
        @media only screen and (max-width: 480px) { .container { width: 100%; } }
      </style>
    </head>
    <body>
      <div style="display:none">Preheader: Save 20% today!</div>
      <table class="container">
        <tr><td>
          <h1>Your Bill Is Ready</h1>
          <p>Your National Grid bill of $123.45 is now available.</p>
          <a href="https://example.com/pay">Pay Now</a>
        </td></tr>
      </table>
      <img src="https://tracker.example.com/pixel.gif" width="1" height="1">
    </body>
    </html>
    """
    out = html_to_text(html)
    assert "Your Bill Is Ready" in out
    assert "$123.45" in out
    assert "[Pay Now](https://example.com/pay)" in out
    assert "Preheader: Save 20% today!" not in out
    assert "{" not in out
    assert "margin:" not in out
    assert "background-color" not in out
    assert "@media" not in out
    assert "tracker.example.com" not in out
