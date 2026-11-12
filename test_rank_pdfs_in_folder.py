import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

import rank_pdfs_in_folder as rpf


class RankPdfsInFolderTests(unittest.TestCase):
    def test_default_is_latest_scimago_edition_released_in_2026(self):
        args = rpf.build_parser().parse_args([])
        self.assertEqual(args.rank_year, 2025)
        self.assertEqual(args.cache_dir, rpf.jrl.DEFAULT_CACHE_DIR)

    def test_find_dois_filters_issn_pseudo_doi(self):
        text = "10.1111/(ISSN)1540-5915 and https://doi.org/10.1111/deci.12620"
        self.assertEqual(rpf.find_dois(text), ["10.1111/deci.12620"])

    def test_filename_title_cleanup(self):
        path = Path("09_2024_Huang_Digital_Twins_food_supply_chain_review.pdf")
        self.assertEqual(
            rpf.title_from_filename(path),
            "Huang Digital Twins food supply chain review",
        )

    def test_extracts_doi_from_pdf_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            writer.add_metadata(
                {
                    "/Title": "Example paper title",
                    "/Subject": "doi:10.1016/j.cie.2020.106645",
                }
            )
            with path.open("wb") as stream:
                writer.write(stream)
            reader = rpf.PdfReader(str(path))
            candidates, title, _ = rpf.collect_opening_candidates(
                reader, opening_pages=1
            )
        self.assertEqual(title, "Example paper title")
        self.assertEqual(candidates[0].doi, "10.1016/j.cie.2020.106645")
        self.assertEqual(candidates[0].evidence_score, 100)

    def test_csv_headers_are_exact(self):
        self.assertEqual(
            rpf.OUTPUT_FIELDS,
            [
                "Paper title",
                "DOI",
                "Journal",
                "Publication year",
                "SCImago ranking year",
                "Journal quartile",
            ],
        )


if __name__ == "__main__":
    unittest.main()
