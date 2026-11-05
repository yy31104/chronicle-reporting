# ADR 0002: Explicit time and exact numeric contracts

Status: adopted.

Every query pins an explicit snapshot. A context resolver may read the current pointer once and persist the chosen identity. Source-as-of queries use a date-only exclusive cutoff; receipt times are separate UTC instants.

A later, higher-order unknown-time revision blocks an authoritative historical value. Missing and withdrawn winners never fall back to an older number.

Parse source decimals into scaled integers, retain exact arithmetic, and round once using remainder-based half-even rounding to twelve places. Independent Fraction-based tests use their own rounding implementation. The numeric bounds and error codes are specified in [the working contract](../WORKING-CONTRACT.md).
