# Archive examples

The twelve numerical cells in `archive/releases.json` come from the U.S. Bureau of Labor Statistics Employment Situation releases of February 2 and March 8, 2024 (Table B-1, total nonfarm, seasonally adjusted), and the Board of Governors of the Federal Reserve System G.17 releases of February 15 and March 15, 2024 (Summary, total index, seasonally adjusted, 2017=100).

The JSON preserves release dates, source URLs, retained HTML hashes and extraction provenance. It contains publisher-produced public-domain numerical material. Logos, seals, photographs, illustrations and third-party material are excluded. The software MIT license does not change separate publisher notices. See the [BLS notice](https://www.bls.gov/bls/linksite.htm) and [Board notice](https://www.federalreserve.gov/disclaimer.htm).

Raw browser-saved BLS HTML is not distributed. The BLS parser fixtures are synthetic structural examples and must not be imported as genuine source acquisitions. Federal Reserve fixtures retain only the selected numerical tables. RTDSM workbooks and reference tables are not included; use locally supplied files under their own applicable terms.

`python scripts/revision_statistics_report.py` derives the committed descriptive report without network access or a database. It never replaces a missing observation with zero. All first-release and horizon limitations are retained in the output.
