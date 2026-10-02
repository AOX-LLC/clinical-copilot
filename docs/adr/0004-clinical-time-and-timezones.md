# 0004. Keep the precision the source gave; one practice timezone

Status: Accepted

## Context
- FHIR times may be a year, a month, a day, or an instant with a UTC offset.
- Healthie returns datetimes in the authenticated user's timezone ([Healthie docs](https://docs.gethealthie.com/guides/api-concepts/timezones/)).

The classic bug is storing a date-only lab as midnight UTC: every US user then sees it on the previous day. Two clinicians in different zones must also never see different dates for the same event.

## Decision
- **Instants** are stored as UTC `timestamptz`, with the original text kept in `occurred_raw`.
- **Year, month and day** precision are stored as a calendar `date` plus `time_precision`. Nothing invents a time of day or a timezone.
- **Check constraints** tie each precision to its column: an instant has `occurred_at`; a calendar precision has `occurred_on`; `unknown` has neither.
- **A time with a clock but no offset is rejected** at the adapter boundary, as FHIR requires an offset in that case.
- **One practice timezone** (`CLINIC_TIMEZONE`, IANA) drives ordering, display dates, "today", ages and alert due dates. Calendar-precision events sort at the start of their day in that zone.
- **Birth dates stay dates.** Ages are computed in the practice timezone.
- **Labs keep collection, result and receipt times separately.** Trends and deltas use collection time.
- **`CLINIC_TODAY` can fix "today"** so demos, alerts and evals reproduce. Synthea data is generated against a pinned reference date.

## Consequences
- A date-only event shows on the same calendar date in every zone. Property tests cover zones on both sides of the date line.
- Display code must branch on precision ("March 2026" versus "8 March 2026, 09:15").
- If the practice timezone changes, the derived `sort_at` column must be recomputed.
