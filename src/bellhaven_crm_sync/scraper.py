"""Scrape the assessment homepage, paginated directory, and community detail pages."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from .models import Community
from .http_utils import trusted_ssl_context

WEBSITE_BASE = "https://analyst-assessment-production.up.railway.app"
USER_AGENT = "BellhavenCRMReview/1.0 (assessment project)"


def fetch_page(url: str) -> str:
    """Fetch one public page over verified HTTPS and retain the server's text encoding."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        # Certificate validation stays enabled; a failed fetch is not silently retried
        # with weaker TLS settings because scraped data informs CRM proposals.
        with urllib.request.urlopen(request, timeout=20, context=trusted_ssl_context()) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset)
    except (urllib.error.URLError, TimeoutError) as error:
        # Add the URL to the error so an operator can identify the failed page.
        raise RuntimeError(f"Could not fetch assessment website page {url}: {error}") from error


class DirectoryParser(HTMLParser):
    """Collect community links and pagination links from directory HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Keep detail and pagination links separately so the caller can follow
        # directory navigation without treating it as a community record.
        self.community_urls: list[str] = []
        self.page_links: list[str] = []
        self._active_href: str | None = None
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if not href:
            # Anchors without destinations cannot be fetched.
            return
        if href.startswith("/communities/"):
            # Only keep links with visible anchor text; navigation links may share this prefix.
            self._active_href = href
            self._active_text = []
        elif href.startswith("/communities?page="):
            self.page_links.append(href)

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href is not None:
            if "".join(self._active_text).strip():
                # Ignore empty links, which are generally navigation artifacts.
                self.community_urls.append(self._active_href)
            # Reset parser state after every completed anchor.
            self._active_href = None
            self._active_text = []


class CommunityDetailParser(HTMLParser):
    """Capture labeled detail values and care badges without a brittle HTML dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.details: dict[str, str] = {}
        self.badges: list[str] = []
        self.capture_title = False
        self.capture_badge = False
        self.capture_value = False
        self.label_parts: list[str] = []
        self.value_parts: list[str] = []
        self.current_label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        if tag == "h1":
            # The page title is the authoritative public-facing community name.
            self.capture_title = True
        elif tag == "dt":
            # Start a fresh label; the next dd value will be stored under it.
            self.label_parts = []
        elif tag == "dd":
            # Each detail value is gathered independently from its label.
            self.capture_value = True
            self.value_parts = []
        elif tag == "span" and "badge" in classes.split():
            # Only spans styled as badges are considered care offerings.
            self.capture_badge = True
        elif tag == "br" and self.capture_value:
            self.value_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.capture_title:
            self.title_parts.append(data)
        elif self.capture_badge:
            if data.strip():
                self.badges.append(data.strip())
        elif self.capture_value:
            self.value_parts.append(data)
        # dt text is accumulated outside dd blocks, then committed when its closing tag arrives.
        elif self.label_parts is not None:
            self.label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.capture_title = False
        elif tag == "dt":
            # Remember the normalized label so the following dd can be indexed.
            self.current_label = "".join(self.label_parts).strip().lower()
        elif tag == "dd":
            if self.current_label:
                # A br is converted to a newline so street and locality stay separate.
                self.details[self.current_label] = "".join(self.value_parts).strip()
            self.capture_value = False
        elif tag == "span":
            # End badge capture so adjacent page text is not included.
            self.capture_badge = False


def parse_community_detail(url: str, html: str) -> Community:
    """Parse and validate the required fields from a single community page."""
    parser = CommunityDetailParser()
    parser.feed(html)
    name = " ".join("".join(parser.title_parts).split())
    address_lines = [line.strip() for line in parser.details.get("address", "").splitlines() if line.strip()]
    if not name or len(address_lines) < 2:
        # A partial page must not become a proposal with guessed location fields.
        raise ValueError(f"Community page did not contain a title and address: {url}")

    # Fail loudly on unexpected address formats rather than creating incomplete CRM proposals.
    match = re.fullmatch(r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", address_lines[-1])
    if not match:
        # Accept only an explicit city, two-letter state, and ZIP/ZIP+4 format.
        raise ValueError(f"Could not parse city, state, and postal code from {url}")
    return Community(
        url=url,
        name=name,
        address=address_lines[0],
        city=match.group(1).strip(),
        state=match.group(2),
        postal_code=match.group(3),
        care_offerings=tuple(dict.fromkeys(parser.badges)),
    )


def scrape_communities(base_url: str = WEBSITE_BASE) -> list[Community]:
    """Return unique communities linked from the homepage and every directory page."""
    base_url = base_url.rstrip("/")
    page_number = 1
    visited_pages: set[str] = set()
    detail_paths: list[str] = []
    # The homepage may feature a newly added location not yet present in the paginated directory.
    homepage = DirectoryParser()
    homepage.feed(fetch_page(f"{base_url}/"))
    detail_paths.extend(homepage.community_urls)

    # Follow server-provided page numbers instead of assuming a fixed directory length.
    while True:
        directory_url = f"{base_url}/communities?page={page_number}"
        if directory_url in visited_pages:
            # Protect against a malformed pagination link that points backward.
            break
        visited_pages.add(directory_url)
        parser = DirectoryParser()
        parser.feed(fetch_page(directory_url))
        detail_paths.extend(parser.community_urls)
        next_pages = [
            int(query_value)
            for link in parser.page_links
            for query_value in [urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("page", [""])[0]]
            if query_value.isdigit() and int(query_value) > page_number
        ]
        if not next_pages:
            # No forward page link means the directory has ended.
            break
        page_number = min(next_pages)

    # Preserve the site's order while avoiding duplicate requests for featured locations.
    unique_paths = list(dict.fromkeys(detail_paths))
    if not unique_paths:
        # Empty results can mean a site-layout change; fail instead of proposing
        # that every CRM account is stale based on a broken scrape.
        raise RuntimeError("The community directory returned no community links.")
    communities = [
        parse_community_detail(
            urllib.parse.urljoin(f"{base_url}/", path),
            fetch_page(urllib.parse.urljoin(f"{base_url}/", path)),
        )
        for path in unique_paths
    ]
    return communities
