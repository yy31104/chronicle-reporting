# Direct archive source contract

Version: `chronicle.source.v1`, 2026-10-29.

Chronicle's real-source adapter accepts exactly four source IDs, mapped in code to
these HTTPS archive URLs:

| ID | Source |
|---|---|
| `bls-2024-02-02` | [BLS Employment Situation, February 2](https://www.bls.gov/news.release/archives/empsit_02022024.htm) |
| `bls-2024-03-08` | [BLS Employment Situation, March 8](https://www.bls.gov/news.release/archives/empsit_03082024.htm) |
| `frb-2024-02-15` | [Board G.17, February 15](https://www.federalreserve.gov/releases/g17/20240215/) |
| `frb-2024-03-15` | [Board G.17, March 15](https://www.federalreserve.gov/releases/g17/20240315/) |

No FRED/ALFRED acquisition is enabled. No arbitrary URL, crawl, credential, redirect
or external image/script is followed. urllib performs normal HTTPS certificate
verification. Each publisher has one outstanding request per process; responses
have a 5 MiB byte limit, a 20-second socket timeout, and a 30-second wall deadline
checked between reads. An in-progress read may finish at its socket timeout before
the wall-deadline check. Non-HTML, compressed, oversized or truncated responses
fail without producing an import artifact. Network failures do not substitute
synthetic numbers or results from another channel.

## Scope of the rights review

The [BLS policy](https://www.bls.gov/bls/linksite.htm) identifies its published work
as public domain, with exceptions for previously copyrighted images, and requests
attribution. Its emblem has a separate restriction. The [Board policy](https://www.federalreserve.gov/disclaimer.htm)
permits reuse of its information unless otherwise indicated, requests attribution,
and treats third-party material and official insignia separately.

The versioned policy in `src/chronicle/resources/sources-rights-v1.json` records the project maintainer, review time, evidence URI, scope and exclusions. It permits local ingestion, analysis and evaluation fixtures for the selected numeric material. Broader runtime exports remain disabled by that policy. Repository examples contain only the attributed publisher-produced numeric cells described in [data notices](../data/README.md). This scoped software policy is not legal advice.
Source citation remains in every returned manifest. A revoked/unknown policy,
missing required use or expanded use rejects ingestion. Retention and current
rights checks at reading/export time belong to the object/publication service.

## Parser, order and provenance

`fetch_and_parse(source_id, batch_id, policy=None)` returns a `SourceArtifact` with
raw bytes, typed immutable revisions and a manifest. It does not publish, create
database rows or mutate a snapshot. `parse_payload` accepts retained bytes and
their actual receipt timestamp for controlled replay; callers are responsible for
authenticity of those retained bytes and timestamp. Raw HTML is retained locally
for parser provenance; excluded external media are not downloaded or exposed.

The manifest contains the raw SHA256 and size, exact URI, publisher, parser version,
actual UTC receipt, source release date, date-only precision, source units/display
precision, rights policy, attribution, and exact table/row/header/cell locators.
Thousands grouping is removed only after validating the BLS-specific lexical
grammar; original cell text remains in evidence. Source preliminary/revised
markers remain in cell evidence. Missing cells become explicit null `missing`
revisions; no missing value becomes zero. Units, table labels, required calendar
columns, numeric syntax and release-date assertions are checked and drift fails
closed for quarantine by the caller.

BLS selects Table B-1's **seasonally adjusted** total-nonfarm levels, retaining
`thousand_persons` units. Adjacent unadjusted columns are never accepted as SA.
Board selects the current total-index row in the Summary table, under `2017=100`
and seasonally adjusted units, excluding the separate Previous estimates row and
percent-change columns. The adapter intentionally imports only the 12 reviewed
cells, not the full histories of either series.

The namespaces are `bls.ces.archive` / series `employment` and `frb.g17.archive` /
series `production`. The immutable source revision key is the source ID; order is
the evidenced release date's ordinal. Both availability and ordering are checked
against that date, not local receipt or URL lexical order. Unknown dates, inverted
order/date pairs and distinct releases sharing an ordinal are rejected. A future
same-day corrected release requires separately evidenced ordering and a new
adapter contract. BLS's 08:30 ET text is retained only as release evidence; the
adapter promises calendar-day availability, not first network availability.

Repeated retrieval of the same release is a new observation, not necessarily a new
source revision. A new request with changed HTML bytes retains a distinct raw
object and actual receipt. When all immutable parsed revision fields match, the
revision is deduplicated and the new occurrence is added in its own batch. Old
snapshot membership and report bytes remain unchanged. Changed immutable fields
under the same source revision identity are rejected; changing navigation HTML
does not permit silently replacing an already known cell. Retrying the same
request uses the retained acquisition rather than retrieving the page again.

## Operator-supplied original HTML

`ingestion.import_sources(..., local_files={source_id: Path(...)})` accepts a
bounded saved HTML file for any requested allowlisted source ID. The CLI exposes
this through repeatable `--source-file SOURCE_ID=PATH` options. The expected
official URL is fixed by that source ID. The operator must supply the original
document saved from that URL; this route does not accept a PDF, a mirror, a web
reader excerpt or a modeled fixture as authenticated publisher HTML.

The reader accepts regular files only, rejects symbolic-link paths and named
pipes, and reads no more than 5 MiB plus one limit-detection byte. It records the
actual UTC time after the local read, never the file modification time or release
date. The same table, seasonal-adjustment, unit, release and cell checks run as for
network acquisition. Local paths are not retained in manifests or error messages.

The manifest labels the acquisition channel
`operator_supplied_direct_publisher_html`, method `local_file`, and authenticity
`operator_asserted_not_independently_authenticated`. A hash establishes which
bytes were supplied; it does not establish who published them. This explicit
trust boundary survives parser repair into a new generation. Existing reviewed
rights scopes apply; this is not a means to approve another distributor's terms.

The request document binds the method, source URI and byte hash. A retry must supply
the same bytes, though the local filename may change; changed bytes or a switch
between network and file acquisition return `INGESTION_IDENTITY_CONFLICT`. Once
acquisition was retained, retry uses its original receipt and parsed rows. The
file is reread to verify request identity, including for a completed request, so
it must remain available for this CLI retry path. Report replay needs no such file.

## Verification and limits

The archive example retains five BLS and seven Federal Reserve cells with publisher URLs, release dates, hashes and provenance in `data/archive/releases.json`. The January employment example is353 to229 thousand persons. The two production windows are102.7/102.7/102.6 and103.0/102.7/102.2. The adapter selects only the explicitly reviewed months.

The source tests cover adjacent-column poison, previous-estimate exclusion, source/unit/date/header drift, missing cells, rights denial, actual receipt semantics, byte limits, truncation, redirects and HTTP failure. Saved-file tests cover provenance, file safety, retry identity and parser repair. Controlled test responses are not evidence that a publisher endpoint is reachable from every deployment.

A policy document is serialized canonically and hashed by the object-registration SQL. Updating reviewer metadata changes that policy identity. Newly staged objects and snapshot manifests must use the new identity; historical policy hashes are never rewritten in an existing database. `scripts/refresh_resources.py` regenerates the checked fixture checksums and resource inventory. PostgreSQL tests check the actual database hash and manifest references.
