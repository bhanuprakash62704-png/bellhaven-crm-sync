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
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20, context=trusted_ssl_context()) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset)
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Could not fetch assessment website page {url}: {error}") from error


class DirectoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
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
            return
        if href.startswith("/communities/"):
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
                self.community_urls.append(self._active_href)
            self._active_href = None
            self._active_text = []


class CommunityDetailParser(HTMLParser):
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
            self.capture_title = True
        elif tag == "dt":
            self.label_parts = []
        elif tag == "dd":
            self.capture_value = True
            self.value_parts = []
        elif tag == "span" and "badge" in classes.split():
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
        elif self.label_parts is not None:
            self.label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.capture_title = False
        elif tag == "dt":
            self.current_label = "".join(self.label_parts).strip().lower()
        elif tag == "dd":
            if self.current_label:
                self.details[self.current_label] = "".join(self.value_parts).strip()
            self.capture_value = False
        elif tag == "span":
            self.capture_badge = False


def parse_community_detail(url: str, html: str) -> Community:
    parser = CommunityDetailParser()
    parser.feed(html)
    name = " ".join("".join(parser.title_parts).split())
    address_lines = [line.strip() for line in parser.details.get("address", "").splitlines() if line.strip()]
    if not name or len(address_lines) < 2:
        raise ValueError(f"Community page did not contain a title and address: {url}")

    match = re.fullmatch(r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", address_lines[-1])
    if not match:
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
    base_url = base_url.rstrip("/")
    page_number = 1
    visited_pages: set[str] = set()
    detail_paths: list[str] = []
    homepage = DirectoryParser()
    homepage.feed(fetch_page(f"{base_url}/"))
    detail_paths.extend(homepage.community_urls)

    while True:
        directory_url = f"{base_url}/communities?page={page_number}"
        if directory_url in visited_pages:
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
            break
        page_number = min(next_pages)

    unique_paths = list(dict.fromkeys(detail_paths))
    if not unique_paths:
        raise RuntimeError("The community directory returned no community links.")
    communities = [
        parse_community_detail(
            urllib.parse.urljoin(f"{base_url}/", path),
            fetch_page(urllib.parse.urljoin(f"{base_url}/", path)),
        )
        for path in unique_paths
    ]
    return communities
