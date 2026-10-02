# 0014. How field encryption is built: key layout, blind indexes and what is not yet wired

Status: Accepted. Implements [0008](0008-field-level-encryption.md) and settles the choices it left open.

## Context
[0008](0008-field-level-encryption.md) chose AES-256-GCM, per-patient data keys wrapped by a key-encryption key (KEK), and HMAC blind indexes. Building it needed answers to the questions the record left open: where wrapped keys live, what the version byte means, how names are indexed, and which database role may do what.

## Decision
- **Cipher.** A sealed value is `version byte || 12-byte random nonce || ciphertext || 16-byte tag`, 29 bytes of overhead.
  - The associated data is a fixed domain label plus table, column and row id. A ciphertext moved to another row, column, table or patient fails to open.
  - The version byte is the data-key generation, currently 1. Rotating a KEK re-wraps data keys and never touches a field ciphertext; a future re-key of a patient's data would write version 2 beside version 1.
- **Keys in the database.** `data_key` has one row per patient, plus one system key for records with no patient subject (practitioners, organizations). A partial unique index allows exactly one system key.
  - `wrapped_key` is the data key sealed with AES-256-GCM under the KEK. Its associated data binds the owner and the KEK version, so a wrapped key cannot be moved to another patient or passed off as another version.
  - `kek_version` records which KEK wraps the row. A run holding another version refuses to load the key rather than guess.
  - Destroying a key sets `wrapped_key` to NULL and `destroyed_at`; a check constraint ties the two together. The application role may only read and insert `data_key`, so destruction is an owner-role act. After it, nothing in the database can open that patient's sealed fields, and the same transaction deletes the patient's blind-index rows so the patient can no longer be found by name, birth date or identifier.
- **Keys outside the database.** `FIELD_KEK` and `BLIND_INDEX_KEY` are 32 random bytes, base64-encoded, from the environment or a secret file (`NAME` or `NAME_FILE`, exactly one). They must differ, errors name the variable and never its value, and the key objects hide their bytes from `repr`.
- **In a run.** Data keys are unwrapped into an in-memory key ring before a transaction seals or opens anything, because the sealer interface is synchronous. Two runs creating the same key at once converge on one row.
- **Blind indexes.** `patient_blind_index` holds one HMAC-SHA256 digest per name token, birth date and identifier, keyed by `BLIND_INDEX_KEY` and domain-separated by kind. It replaces the single `patient.name_bidx` column.
  - Names are split into letter runs, folded to NFKC lower case and stripped of digits, so Synthea's `Abe604` and a typed `abe` meet.
  - Lookup is exact: every token of a search name must be present, there is no prefix or fuzzy matching, and a different blind-index key finds nothing.
  - The application role may insert and delete index rows (they are rebuilt when demographics change) and may not update them.
- **What is sealed in the patient row.** Given names (space-joined), family name, ISO birth date and identifiers (a JSON list of system and value pairs). `sex_at_birth` stays plaintext, as 0008 decided. A patient is inserted with empty encrypted fields first, because a data key refers to the patient row, and the fields are sealed in the same transaction.
- **One production sealer.** `FieldSealer` implements `PayloadSealer`. The stand-in sealer used in Phase 1 tests is not constructed in application code, and a test fails if production code mentions it.

## Not built yet
- The ingest command that seals source payloads and timeline fields with `FieldSealer`. The ingest function already takes the sealer; the command that supplies it is the next piece of Phase 2.
- A KEK rotation command. The re-wrap step exists and is tested; nothing runs it over the table.
- Key custody beyond a secret file: a KMS or mounted secret in a deployment (Phase 5).

## Consequences
- Losing the KEK makes every encrypted field unreadable, and a key handed out with a database dump opens nothing without it.
- Changing `BLIND_INDEX_KEY` breaks lookup until the indexes are rebuilt from the sealed identities, which needs the data keys.
- Destroying a patient's key is not erasure of everything. It makes the sealed fields unreadable and removes the blind-index rows, but the plaintext timeline columns (codes, values, times, by 0008's design) and `patient_source_link.external_id` remain. A backup holding the wrapped key or the index rows can bring them back, so backups must be retired on the same schedule as the data.
- Blind-index digests cover low-entropy values: a birth date has about 36,500 candidates per century, an SSN-shaped identifier about a billion, a name token comes from a dictionary. Whoever holds `BLIND_INDEX_KEY` and a copy of the index rows can recover them by enumeration, so that key needs the same custody as the KEK. This is also why destruction deletes the rows.
- Digests are the same for every patient, so the index reveals frequency (a common surname, a shared birthday) to anyone who can read the table.
- A name search reads index rows only, so it never decrypts anything. Showing the matched patient's name decrypts one row.
- Removing digits from name tokens means two names that differ only by digits are indistinguishable to lookup. Real names do not carry digits; the synthetic ones do.
