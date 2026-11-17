import tempfile
import unittest
from pathlib import Path

import download_zotero_pdfs_by_tag as dzp


class DownloadZoteroPdfsByTagTests(unittest.TestCase):
    def test_pdf_attachment_detection(self):
        self.assertTrue(
            dzp.is_pdf_attachment(
                {
                    "itemType": "attachment",
                    "contentType": "application/pdf",
                    "filename": "paper.pdf",
                }
            )
        )
        self.assertFalse(dzp.is_pdf_attachment({"itemType": "note"}))

    def test_file_url_to_path_decodes_spaces(self):
        self.assertEqual(
            dzp.file_url_to_path("file:///tmp/My%20Paper.pdf"),
            Path("/tmp/My Paper.pdf"),
        )

    def test_identical_destination_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            output_dir = root / "output"
            source_dir.mkdir()
            output_dir.mkdir()
            source = source_dir / "paper.pdf"
            source.write_bytes(b"same PDF")
            (output_dir / "paper.pdf").write_bytes(b"same PDF")
            destination, note = dzp.destination_for(
                source, output_dir, "ABCD2345", "paper.pdf"
            )
        self.assertEqual(destination.name, "paper.pdf")
        self.assertEqual(note, "identical file already exists")

    def test_collision_appends_attachment_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            output_dir = root / "output"
            source_dir.mkdir()
            output_dir.mkdir()
            source = source_dir / "paper.pdf"
            source.write_bytes(b"new PDF")
            (output_dir / "paper.pdf").write_bytes(b"different PDF")
            destination, note = dzp.destination_for(
                source, output_dir, "ABCD2345", "paper.pdf"
            )
        self.assertEqual(destination.name, "paper_ABCD2345.pdf")
        self.assertIn("collision", note)

    def test_defaults(self):
        args = dzp.build_parser().parse_args([])
        self.assertEqual(args.tag, "Rank:Q3")
        self.assertEqual(args.output_dir, dzp.DEFAULT_OUTPUT_DIR)


if __name__ == "__main__":
    unittest.main()
