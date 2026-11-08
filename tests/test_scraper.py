"""Parser and pagination tests backed by small HTML strings, not network requests."""

import unittest
import urllib.error
from unittest.mock import patch

from bellhaven_crm_sync.scraper import (
    CommunityDetailParser,
    DirectoryParser,
    fetch_page,
    parse_community_detail,
    scrape_communities,
)


DETAIL_PAGE = """<!doctype html><html><body>
<div class="wrap"><h1>Bellhaven of Maplewood</h1>
<dl class="detail">
<dt>Address</dt><dd>210 Orchard Lane<br>Maplewood, OH 44280</dd>
<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>
</dl></div></body></html>"""


class ScraperTests(unittest.TestCase):
    """Cover both community detail extraction and homepage/directory link discovery."""

    def test_parse_community_detail_extracts_required_fields(self):
        # Preserve address line breaks and collect every care badge.
        community = parse_community_detail("https://example.test/communities/maplewood", DETAIL_PAGE)

        self.assertEqual(community.name, "Bellhaven of Maplewood")
        self.assertEqual(community.address, "210 Orchard Lane")
        self.assertEqual(community.city, "Maplewood")
        self.assertEqual(community.state, "OH")
        self.assertEqual(community.postal_code, "44280")
        self.assertEqual(community.care_offerings, ("Assisted Living", "Memory Support"))

    def test_scrape_communities_follows_all_directory_pages(self):
        # The featured homepage link overlaps the directory; output should include it once.
        pages = {
            "https://example.test/": (
                '<a href="/communities/findlay">Featured community</a>'
            ),
            "https://example.test/communities?page=1": (
                '<a href="/communities/maplewood">Maplewood</a>'
                '<a href="/communities?page=2">Next</a>'
            ),
            "https://example.test/communities?page=2": (
                '<a href="/communities/findlay">Findlay</a>'
            ),
            "https://example.test/communities/maplewood": DETAIL_PAGE,
            "https://example.test/communities/findlay": DETAIL_PAGE.replace(
                "Bellhaven of Maplewood", "Bellhaven Meadows of Findlay"
            ).replace("210 Orchard Lane", "1800 N Blanchard St").replace(
                "Maplewood, OH 44280", "Findlay, OH 45840"
            ),
        }

        with patch("bellhaven_crm_sync.scraper.fetch_page", side_effect=pages.__getitem__):
            communities = scrape_communities("https://example.test")

        self.assertEqual([community.name for community in communities], [
            "Bellhaven Meadows of Findlay",
            "Bellhaven of Maplewood",
        ])

    def test_parser_decodes_entities_and_collects_only_nonempty_community_links(self):
        parser = DirectoryParser()
        parser.feed(
            '<a href="/communities/a">A &amp; B</a>'
            '<a href="/communities/empty"></a>'
            '<a href="/communities?page=2">Next</a>'
        )

        self.assertEqual(parser.community_urls, ["/communities/a"])
        self.assertEqual(parser.page_links, ["/communities?page=2"])

    def test_parser_handles_address_zip_plus_four(self):
        html = DETAIL_PAGE.replace("Maplewood, OH 44280", "Maplewood, OH 44280-1234")

        community = parse_community_detail("https://example.test/location", html)

        self.assertEqual(community.postal_code, "44280-1234")

    def test_parser_rejects_missing_name_or_address(self):
        cases = (
            "<html><body><h1>Facility</h1></body></html>",
            "<html><body><dl><dt>Address</dt><dd>1 Main St</dd></dl></body></html>",
        )
        for html in cases:
            with self.subTest(html=html):
                with self.assertRaisesRegex(ValueError, "title and address"):
                    parse_community_detail("https://example.test/location", html)

    def test_parser_rejects_malformed_locality_line(self):
        html = DETAIL_PAGE.replace("Maplewood, OH 44280", "Maplewood Ohio")

        with self.assertRaisesRegex(ValueError, "Could not parse city"):
            parse_community_detail("https://example.test/location", html)

    def test_parser_deduplicates_repeated_care_badges(self):
        html = DETAIL_PAGE.replace(
            '<span class="badge">Memory Support</span>',
            '<span class="badge">Assisted Living</span>',
        )

        community = parse_community_detail("https://example.test/location", html)

        self.assertEqual(community.care_offerings, ("Assisted Living",))

    def test_scraper_fails_closed_when_directory_has_no_community_links(self):
        with patch(
            "bellhaven_crm_sync.scraper.fetch_page",
            side_effect=["<html></html>", "<html>No locations</html>"],
        ):
            with self.assertRaisesRegex(RuntimeError, "no community links"):
                scrape_communities("https://example.test")

    def test_scraper_stops_when_pagination_does_not_move_forward(self):
        homepage = '<a href="/communities/one">One</a>'
        directory = (
            '<a href="/communities/one">One</a>'
            '<a href="/communities?page=1">Previous</a>'
        )
        pages = {
            "https://example.test/": homepage,
            "https://example.test/communities?page=1": directory,
            "https://example.test/communities/one": DETAIL_PAGE,
        }
        with patch(
            "bellhaven_crm_sync.scraper.fetch_page",
            side_effect=pages.__getitem__,
        ) as fetch:
            results = scrape_communities("https://example.test")

        self.assertEqual(len(results), 1)
        self.assertEqual(fetch.call_count, 3)

    def test_fetch_page_translates_url_errors_with_page_context(self):
        with patch(
            "bellhaven_crm_sync.scraper.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaisesRegex(RuntimeError, "https://example.test/page"):
                fetch_page("https://example.test/page")

    def test_detail_parser_collects_multiple_badges_and_labels(self):
        parser = CommunityDetailParser()
        parser.feed(DETAIL_PAGE)

        self.assertEqual(parser.current_label, "care offerings")
        self.assertEqual(parser.details["address"], "210 Orchard Lane\nMaplewood, OH 44280")
        self.assertEqual(parser.badges, ["Assisted Living", "Memory Support"])


if __name__ == "__main__":
    unittest.main()
