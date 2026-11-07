import unittest
from unittest.mock import patch

from bellhaven_crm_sync.scraper import parse_community_detail, scrape_communities


DETAIL_PAGE = """<!doctype html><html><body>
<div class="wrap"><h1>Bellhaven of Maplewood</h1>
<dl class="detail">
<dt>Address</dt><dd>210 Orchard Lane<br>Maplewood, OH 44280</dd>
<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>
</dl></div></body></html>"""


class ScraperTests(unittest.TestCase):
    def test_parse_community_detail_extracts_required_fields(self):
        community = parse_community_detail("https://example.test/communities/maplewood", DETAIL_PAGE)

        self.assertEqual(community.name, "Bellhaven of Maplewood")
        self.assertEqual(community.address, "210 Orchard Lane")
        self.assertEqual(community.city, "Maplewood")
        self.assertEqual(community.state, "OH")
        self.assertEqual(community.postal_code, "44280")
        self.assertEqual(community.care_offerings, ("Assisted Living", "Memory Support"))

    def test_scrape_communities_follows_all_directory_pages(self):
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


if __name__ == "__main__":
    unittest.main()
