# DOI journal-quartile lookup

`journal_rank_lookup.py` accepts a list of paper DOI links (or raw DOIs),
resolves each paper through Crossref, and reports the **journal's SCImago best
quartile** for a selected year.

> A paper itself is not Q1, Q2, Q3, or Q4. The quartile belongs to the journal
> and may differ by database, subject category, and year. This script reports
> the `SJR Best Quartile` field from the SCImago/Scopus-based export—not a
> Clarivate JCR quartile.

The script uses only the Python standard library.

## Quick start

```bash
python3 journal_rank_lookup.py \
  https://doi.org/10.1016/j.ejor.2024.03.026 \
  https://doi.org/10.1016/j.cie.2024.110616 \
  --rank-year 2025
```

The ScienceDirect PII form used in the earlier example is supported as well.
The script first asks Elsevier's article API for its DOI and then resolves the
paper through Crossref:

```bash
python3 journal_rank_lookup.py \
  'https://www.sciencedirect.com/science/article/pii/S0377221724002261' \
  --rank-year 2025
```

Basic metadata for open articles may work without credentials. If Elsevier
requires authentication for a particular record, provide an API key without
putting it directly in the command history:

```bash
export ELSEVIER_API_KEY='your-key'
python3 journal_rank_lookup.py \
  'https://www.sciencedirect.com/science/article/pii/S0377221724002261'
```

## Process a file

A plain-text input contains one DOI or link per line:

```bash
python3 journal_rank_lookup.py --input papers.txt --format csv --output ranks.csv
```

A CSV input may have a column named `doi`, `url`, or `link`. If none exists,
the first column is used:

```csv
doi,title
https://doi.org/10.1016/j.ejor.2024.03.026,Warehousing review
https://doi.org/10.1016/j.cie.2024.110616,Digital twin architecture
```

JSON output is also available:

```bash
python3 journal_rank_lookup.py --input papers.csv --format json --output ranks.json
```

## Ranking data

By default, the script tries the official SCImago CSV download and caches the
result in `.cache/journal-rank-lookup/` beside these scripts. SCImago sometimes blocks scripted
downloads, so the script then tries public mirrors of the same yearly export.
For maximum reproducibility, download the desired year manually from
[SCImago Journal Rankings](https://www.scimagojr.com/journalrank.php) and pass
it explicitly:

```bash
python3 journal_rank_lookup.py \
  --input papers.txt \
  --sjr-file '/path/to/scimagojr 2025.csv' \
  --rank-year 2025
```

Matching uses ISSN first and an exact normalized journal title only as a
fallback. The output records the match method, ranking year, SJR value, global
rank, category-specific quartiles (in the `categories` field of CSV/JSON
output), source DOI, and any unresolved cases. The main `quartile` field is
SCImago's **best quartile** for that journal in the selected year.

Crossref recommends including an email address for its polite API pool:

```bash
python3 journal_rank_lookup.py --input papers.txt --mailto you@example.edu
```

## Test

The offline unit tests do not access the network:

```bash
python3 -m unittest -v test_journal_rank_lookup.py
```

## Process every PDF in the References folder

`rank_pdfs_in_folder.py` extracts a DOI from PDF metadata or page text, calls
the same Crossref and SCImago lookup functions, and writes the requested six
CSV columns:

```bash
python3 rank_pdfs_in_folder.py
```

The defaults are:

- PDF folder: `/Users/congdinh/Documents/PhD in Digital Twin/source/References`
- Output: `source/References/journal_quartiles.csv`
- Ranking year: 2025, the latest SCImago edition released in 2026

SCImago labels an edition by the year of its underlying metrics. Therefore,
the latest edition available in 2026 is **SJR 2025**, not SJR 2026.

All paths and the ranking year can be overridden:

```bash
python3 rank_pdfs_in_folder.py \
  --folder '/path/to/pdfs' \
  --output '/path/to/results.csv' \
  --rank-year 2025
```

## Import ranked PDFs into Zotero

`upload_ranked_pdfs_to_zotero.py` reads the PDFs, looks up each journal's
SCImago quartile, creates or updates the matching Zotero journal article, adds
a tag such as `Rank:Q1` instead of changing **Extra**, fills **Abstract** from
Crossref or Semantic Scholar when available, falls back to extracting the
Abstract section from the PDF's opening pages, and uploads the PDF as a stored
attachment.

Before running it, open Zotero 10 or later and enable **Settings → Advanced →
Allow other applications on this computer to communicate with Zotero**. On
the first write, Zotero displays an authorization dialog; choose **Always
Allow** for an uninterrupted batch import.

Preview without changing Zotero:

```bash
python3 upload_ranked_pdfs_to_zotero.py --dry-run
```

Import the default References folder:

```bash
python3 upload_ranked_pdfs_to_zotero.py
```

Import a specific folder:

```bash
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References"
```

Preview that folder without modifying Zotero:

```bash
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References" \
  --dry-run
```

Include PDFs in subfolders:

```bash
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References" \
  --recursive
```

Add one or more Zotero tags to every imported or matched paper:

```bash
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References" \
  --tag "Digital Twin" \
  --tag "Industrial Engineering"
```

Useful options include `--folder`, `--recursive`, `--collection-key`,
`--tag`, `--no-attachments`, `--sjr-file`, and `--report`. Repeat `--tag` to
add multiple tags. The script preserves existing Zotero tags and avoids
case-insensitive duplicates. It matches existing
items by DOI (then exact normalized title), adds or replaces the `Rank:` tag,
fills a blank Abstract when metadata is available, skips a same-named existing
child PDF, and writes a CSV audit report.
PDFs whose DOI or journal cannot be resolved are still imported without a
`Rank:` tag, so a later ranking run can retry them.

## Add rank tags to papers already in Zotero

Preview all top-level Zotero papers that do not already have a tag beginning
with `Rank:`:

```bash
python3 tag_zotero_journal_ranks.py --dry-run
```

Add the tags:

```bash
python3 tag_zotero_journal_ranks.py
```

The script skips the journal-rank lookup for items that already have any
`Rank:` tag. Otherwise, it uses each paper's DOI with
`journal_rank_lookup.py` and adds `Rank:Q1`, `Rank:Q2`, `Rank:Q3`, or
`Rank:Q4`. A journal that resolves successfully but is absent from SCImago (or
has no quartile for the selected year) receives `Rank:NA`. Items without a DOI
or with a temporary lookup failure remain without a rank tag and are recorded
in `zotero_rank_tag_report.csv`.

By default, an existing tag beginning with `Rank:` prevents another rank
lookup, so normal reruns do not recheck ranked papers. To recheck every paper
and replace its existing rank tag, run:

```bash
python3 tag_zotero_journal_ranks.py --recheck-ranks
```

Additional tags can be supplied by repeating `--tag`. Each tag is added only
when that exact text occurs in the paper's Title or Abstract, using
case-insensitive matching. Existing tags are preserved and are not duplicated:

```bash
python3 tag_zotero_journal_ranks.py \
  --tag "Digital Twin" \
  --tag "Industrial Engineering" \
  --tag "Warehouse"
```

Additional tag matching is evaluated for every paper, including papers that
already have a `Rank:` tag.

## Download Zotero PDFs by tag

`download_zotero_pdfs_by_tag.py` defaults to papers tagged `Rank:Q3` and
copies their locally available PDF attachments into `source/References`:

```bash
python3 download_zotero_pdfs_by_tag.py
```

Preview without copying files:

```bash
python3 download_zotero_pdfs_by_tag.py --dry-run
```

The tag and destination can be changed with `--tag` and `--output-dir`. The
script skips identical files and appends the Zotero attachment key when a
different PDF already uses the same filename.

