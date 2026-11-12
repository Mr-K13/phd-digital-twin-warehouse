import unittest

import tag_zotero_journal_ranks as tzr


class ZoteroRankTaggerTests(unittest.TestCase):
    def test_matches_additional_tags_in_title_or_abstract_case_insensitively(self):
        data = {
            "title": "Digital Twin control for warehousing",
            "abstractNote": "An INDUSTRIAL ENGINEERING application.",
        }
        self.assertEqual(
            tzr.matching_additional_tags(
                data,
                ["digital twin", "Industrial Engineering", "Simulation"],
            ),
            ["digital twin", "Industrial Engineering"],
        )

    def test_deduplicates_requested_matching_tags_case_insensitively(self):
        data = {"title": "Digital Twin", "abstractNote": ""}
        self.assertEqual(
            tzr.matching_additional_tags(data, ["Digital Twin", "digital twin"]),
            ["Digital Twin"],
        )

    def test_detects_rank_prefix_case_insensitively(self):
        self.assertTrue(tzr.has_rank_tag([{"tag": "rank:Q2"}]))
        self.assertTrue(tzr.has_rank_tag([{"tag": " Rank:Q1 "}]))
        self.assertFalse(tzr.has_rank_tag([{"tag": "Digital Twin"}]))

    def test_filters_non_paper_items(self):
        self.assertFalse(tzr.is_paper_item({"itemType": "attachment"}))
        self.assertFalse(tzr.is_paper_item({"itemType": "note"}))
        self.assertTrue(tzr.is_paper_item({"itemType": "journalArticle"}))

    def test_uses_project_local_cache(self):
        args = tzr.build_parser().parse_args(
            ["--tag", "Digital Twin", "--tag", "Industrial Engineering"]
        )
        self.assertEqual(args.cache_dir, tzr.jrl.DEFAULT_CACHE_DIR)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.tag, ["Digital Twin", "Industrial Engineering"])


if __name__ == "__main__":
    unittest.main()
