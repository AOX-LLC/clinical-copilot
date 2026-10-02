# 0008. AES-GCM field encryption with per-patient envelope keys

Status: Accepted (columns in Phase 1; encryption built later)

## Context
Disk encryption does not protect against a leaked dump, a backup copied to the wrong place, or a database user reading tables directly. Encrypting every column, though, would make the timeline impossible to query, sort or trend.

## Decision
- **Encrypted columns (suffix `_enc`):**
  - direct identifiers: name, birth date, contact details, address
  - business identifiers: MRN, SSN, licence and passport numbers, which Synthea generates
  - free text: notes, text values, details, summary lines
  - raw source payloads
- **Plaintext columns:**
  - surrogate UUIDs and opaque source ids
  - clinical codes, numeric values, units and reference ranges
  - event times and `sex_at_birth`
  - These are what queries, ordering, trends and alerts need. They are linkable to a person only through the surrogate id.
- **Cipher:**
  - AES-256-GCM, with a one-byte key-version header.
  - Associated data = table, column and row id, so ciphertext cannot be moved between rows or columns.
- **Keys:**
  - Each patient has a data key, wrapped by a key-encryption key held outside the database: a secret file locally, a KMS when deployed.
  - Rotation re-wraps data keys only. Destroying a patient's data key makes all of their encrypted fields unreadable.
  - Records with no patient subject (practitioners, organizations) are sealed under a separate system key class. They hold no patient data, so crypto-shredding never needs to reach them.
  - The sealer receives the patient id with every seal, so it always selects the right key.
- **Lookup:** exact match only, through HMAC blind indexes (normalized name tokens, birth date) with a separate key.
- **pgcrypto was rejected:** keys would travel in SQL text and could land in statement logs.
- **No embeddings of patient free text.** They leak content and cannot be encrypted while staying searchable.

## Consequences
- No `LIKE`, sorting, range queries or JSON queries on encrypted columns. Patient search is exact match. Name sorting happens in the app on one decrypted page, which suits a concierge panel of hundreds.
- Everything the timeline must query is projected to plaintext columns at import.
- About 29 bytes of overhead per value; negligible CPU.
- Migrations cannot transform encrypted data in SQL; a backfill is application code.
- Until the cipher lands, no code path writes patient payloads. Phase 1 tests use a labeled stand-in sealer that is not encryption.
