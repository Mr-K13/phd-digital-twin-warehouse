import unittest

import journal_rank_lookup as jrl
import upload_ranked_pdfs_to_zotero as zri


class ZoteroImporterTests(unittest.TestCase):
    def test_extract_abstract_from_pdf_text(self):
        text = (
            "ARTICLE INFO\nKeywords:\nWarehousing\nABSTRACT\n"
            "Warehouse operations are increasingly un-\nstable under dynamic "
            "disturbances. This study develops a digital twin control approach "
            "and validates it using an industrial case with strong results.\n"
            "1. Introduction\nThis must not be included."
        )
        self.assertEqual(
            zri.abstract_from_text(text),
            "Warehouse operations are increasingly unstable under dynamic "
            "disturbances. This study develops a digital twin control approach "
            "and validates it using an industrial case with strong results.",
        )

    def test_extract_abstract_with_spaced_publisher_heading(self):
        text = (
            "A R T I C L E I N F O\nKeywords:\nDigital Twin\n"
            "A B S T R A C T\nDigital transformation is revolutionizing "
            "warehousing by integrating advanced technologies. This review "
            "examines artificial intelligence, digital twins, and logistics "
            "optimization while identifying future research directions.\n"
            "1. Introduction\nThis must not be included."
        )
        self.assertEqual(
            zri.abstract_from_text(text),
            "Digital transformation is revolutionizing warehousing by "
            "integrating advanced technologies. This review examines artificial "
            "intelligence, digital twins, and logistics optimization while "
            "identifying future research directions.",
        )

    def test_merge_tags_preserves_existing_and_avoids_duplicates(self):
        existing = [
            {"tag": "Digital Twin", "type": 1},
            {"tag": "Reviewed"},
        ]
        self.assertEqual(
            zri.merge_tags(existing, ["digital twin", "Industrial Engineering"]),
            [
                {"tag": "Digital Twin", "type": 1},
                {"tag": "Reviewed"},
                {"tag": "Industrial Engineering"},
            ],
        )

    def test_live_schema_template_builder(self):
        client = zri.ZoteroLocalClient()
        client.get_json = lambda path: [
            {"field": "title"},
            {"field": "DOI"},
            {"field": "extra"},
        ]
        template = client.new_item_template("journalArticle")
        self.assertEqual(template["itemType"], "journalArticle")
        self.assertEqual(template["DOI"], "")
        self.assertEqual(template["creators"], [])

    def test_rank_tag_replaces_previous_rank_and_preserves_other_tags(self):
        existing = [{"tag": "Digital Twin"}, {"tag": "Rank:Q2"}]
        self.assertEqual(
            zri.merge_rank_tag(existing, "Q1"),
            [{"tag": "Digital Twin"}, {"tag": "Rank:Q1"}],
        )

    def test_rank_tag_accepts_na(self):
        self.assertEqual(
            zri.merge_rank_tag([{"tag": "Digital Twin"}], "NA"),
            [{"tag": "Digital Twin"}, {"tag": "Rank:NA"}],
        )

    def test_clean_crossref_jats_abstract(self):
        source = "<jats:p>A &amp; B <jats:bold>digital twin</jats:bold>.</jats:p>"
        self.assertEqual(zri.clean_abstract(source), "A & B digital twin.")

    def test_index_existing_items_normalizes_doi(self):
        item = {
            "data": {
                "key": "ABCD2345",
                "DOI": "https://doi.org/10.1016/J.CIE.2020.106645",
                "title": "Example Paper",
            }
        }
        by_doi, by_title = zri.index_existing_items([item])
        self.assertIs(by_doi["10.1016/j.cie.2020.106645"], item)
        self.assertIs(by_title["examplepaper"], item)

    def test_make_zotero_item_includes_quartile(self):
        template = {
            "itemType": "journalArticle",
            "title": "",
            "creators": [],
            "publicationTitle": "",
            "abstractNote": "",
            "date": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "DOI": "",
            "url": "",
            "ISSN": "",
            "extra": "",
            "collections": [],
        }
        result = jrl.Result(
            input="10.1/test",
            doi="10.1/test",
            paper_title="A paper",
            journal="A Journal",
            publication_year=2024,
            ranking_year=2025,
            quartile="Q1",
            status="ok",
        )
        record = {
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "volume": "12",
            "issue": "3",
            "page": "1-10",
            "ISSN": ["1234-5678"],
            "_resolved_abstract": "A useful abstract.",
        }
        item = zri.make_zotero_item(
            template, result, record, title_fallback="Fallback"
        )
        self.assertEqual(item["creators"][0]["lastName"], "Lovelace")
        self.assertEqual(item["extra"], "")
        self.assertIn({"tag": "Rank:Q1"}, item["tags"])
        self.assertEqual(item["abstractNote"], "A useful abstract.")

    def test_unresolved_paper_gets_no_rank_tag(self):
        template = {
            "itemType": "journalArticle",
            "title": "",
            "creators": [],
            "publicationTitle": "",
            "abstractNote": "",
            "date": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "DOI": "",
            "url": "",
            "ISSN": "",
            "extra": "",
            "collections": [],
        }
        result = jrl.Result(
            input="paper.pdf",
            paper_title="Unknown paper",
            ranking_year=2025,
        )
        item = zri.make_zotero_item(
            template, result, {}, title_fallback="Unknown paper"
        )
        self.assertNotIn("Rank:", " ".join(tag["tag"] for tag in item["tags"]))
        self.assertEqual(item["extra"], "")
        self.assertEqual(item["url"], "")

    def test_parser_defaults_to_latest_scimago_year(self):
        args = zri.build_parser().parse_args(
            ["--tag", "Digital Twin", "--tag", "Industrial Engineering"]
        )
        self.assertEqual(args.rank_year, 2025)
        self.assertEqual(args.tag, ["Digital Twin", "Industrial Engineering"])
        self.assertEqual(args.cache_dir, zri.jrl.DEFAULT_CACHE_DIR)


if __name__ == "__main__":
    unittest.main()
