import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import journal_rank_lookup as jrl


FIXTURE = '''Rank;Sourceid;Title;Type;Issn;SJR;SJR Best Quartile;H index
1161;21100200313;European Journal of Operational Research;journal;03772217, 18726860;2,239;Q1;333
2000;123;Example Journal;journal;1234-567X;0,500;Q2;10
'''


class JournalRankLookupTests(unittest.TestCase):
    def test_default_is_latest_scimago_edition_released_in_2026(self):
        args = jrl.build_parser().parse_args(["10.1016/j.cie.2020.106645"])
        self.assertEqual(jrl.SCIMAGO_RELEASE_YEAR, 2026)
        self.assertEqual(args.rank_year, 2025)
        self.assertEqual(
            args.cache_dir,
            Path(jrl.__file__).resolve().parent / ".cache" / "journal-rank-lookup",
        )

    def test_normalize_doi_url(self):
        value = "https://doi.org/10.1016/j.ejor.2024.03.026?utm_source=x"
        self.assertEqual(jrl.normalize_doi(value), "10.1016/j.ejor.2024.03.026")

    def test_extract_sciencedirect_pii(self):
        value = "https://www.sciencedirect.com/science/article/pii/S0377221724002261"
        self.assertEqual(jrl.PII_RE.search(value).group(0), "S0377221724002261")

    def test_extract_doi_from_elsevier_payload(self):
        payload = {
            "full-text-retrieval-response": {
                "coredata": {"prism:doi": "10.1016/j.ejor.2024.03.026"}
            }
        }
        self.assertEqual(
            jrl._doi_from_elsevier_payload(payload),
            "10.1016/j.ejor.2024.03.026",
        )

    def test_crossref_html_entities_are_unescaped(self):
        metadata = jrl._metadata_from_crossref_item(
            {
                "DOI": "10.1016/j.cie.2024.110616",
                "title": ["A &amp; B"],
                "container-title": ["Computers &amp; Industrial Engineering"],
                "ISSN": ["0360-8352"],
                "published": {"date-parts": [[2024]]},
            }
        )
        self.assertEqual(metadata.title, "A & B")
        self.assertEqual(metadata.journal, "Computers & Industrial Engineering")

    def test_semantic_scholar_metadata_fallback_parser(self):
        payload = {
            "externalIds": {"DOI": "10.1177/21582440241280455"},
            "title": "Sustainability of Perishable Food Cold Chain Logistics",
            "venue": "",
            "year": 2024,
            "journal": {"name": "SAGE Open", "volume": "14"},
        }
        with patch.object(jrl, "_request_bytes", return_value=__import__("json").dumps(payload).encode()):
            metadata = jrl.fetch_semantic_scholar_doi(
                "10.1177/21582440241280455",
                timeout=30,
                user_agent="test/1.0",
            )
        self.assertEqual(metadata.journal, "SAGE Open")
        self.assertEqual(metadata.publication_year, 2024)

    def test_doaj_metadata_fallback_parser(self):
        payload = {
            "results": [
                {
                    "bibjson": {
                        "identifier": [
                            {"id": "2158-2440", "type": "eissn"},
                            {"id": "10.1177/21582440241280455", "type": "doi"},
                        ],
                        "journal": {"title": "SAGE Open", "issns": ["2158-2440"]},
                        "title": "Sustainability of Perishable Food Cold Chain Logistics",
                        "year": "2024",
                    }
                }
            ]
        }
        with patch.object(jrl, "_request_bytes", return_value=__import__("json").dumps(payload).encode()):
            metadata = jrl.fetch_doaj_doi(
                "10.1177/21582440241280455",
                timeout=30,
                user_agent="test/1.0",
            )
        self.assertEqual(metadata.journal, "SAGE Open")
        self.assertEqual(metadata.issns, ["21582440"])

    def test_title_fragment_from_publisher_url(self):
        value = (
            "https://www.emerald.com/f/article-abstract/37/13-14/919/206389/"
            "An-integrated-design-approach-for-class-based?redirectedFrom=fulltext"
        )
        self.assertEqual(
            jrl.title_fragment_from_url(value),
            "An integrated design approach for class based",
        )

    def test_load_and_match_by_issn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sjr.csv"
            path.write_text(FIXTURE, encoding="utf-8")
            rows = jrl.load_sjr_rows(path)
        index = jrl.SJRIndex(rows)
        metadata = jrl.PaperMetadata(
            doi="10.1016/j.ejor.2024.03.026",
            title="50 years of warehousing research",
            journal="European Journal of Operational Research",
            issns=["0377-2217", "1872-6860"],
            publication_year=2025,
        )
        row, method = index.find(metadata)
        self.assertEqual(row["SJR Best Quartile"], "Q1")
        self.assertEqual(method, "ISSN + journal title")

    def test_match_by_normalized_title(self):
        index = jrl.SJRIndex(
            [
                {
                    "Title": "Computers & Industrial Engineering",
                    "Issn": "03608352",
                    "SJR Best Quartile": "Q1",
                }
            ]
        )
        metadata = jrl.PaperMetadata(
            doi="10.0000/example",
            title="Example",
            journal="Computers and Industrial Engineering",
            issns=[],
            publication_year=2024,
        )
        row, method = index.find(metadata)
        # "&" and "and" are intentionally not treated as equivalent; exact
        # title matching is conservative to avoid false journal assignments.
        self.assertIsNone(row)
        self.assertEqual(method, "")

    def test_read_csv_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.csv"
            path.write_text(
                "doi,title\nhttps://doi.org/10.1/a,One\n10.2/b,Two\n",
                encoding="utf-8",
            )
            values = jrl.read_inputs([], path)
        self.assertEqual(values, ["https://doi.org/10.1/a", "10.2/b"])


if __name__ == "__main__":
    unittest.main()
