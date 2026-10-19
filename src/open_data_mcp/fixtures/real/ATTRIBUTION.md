# Attribution for the real-data snapshot

`education_institutions.csv` is a **byte-for-byte copy** of one resource published on
[data.gov.ua](https://data.gov.ua), captured by `scripts/capture_snapshot.py`
(capture time, hashes and metadata: `SNAPSHOT.json`).

- **Dataset:** "Адміністративні дані в значенні Закону України "Про державну статистику", що
  збираються (обробляються) та підлягають оприлюдненню відповідно до вимог закону розпорядником
  інформації"
- **Publisher:** Департамент освіти та науки Івано-Франківської міської ради
- **Dataset page:** https://data.gov.ua/dataset/6598555a-ee52-462c-9344-1a31ba2a1638
- **Resource:** `statistics.csv` (resource id `1950d531-434a-48d8-bdea-1f1542a1800b`, dataset id
  `f2416e7e-8ea7-449c-9dd2-eaf022a5ea1b`)
- **License:** Creative Commons Attribution (CC BY), http://www.opendefinition.org/licenses/cc-by
- **Changes:** none to the file's bytes. Evaluation questions and expected answers are our own
  work, derived from it.

The file lists educational institutions (names, institution type and two numeric columns). It
contains no personal data about individuals; the dataset's maintainer/author fields were
intentionally not copied. The repository's MIT license covers the code only; this file stays under
CC BY with the attribution above.

The data are published as-is by the source and have known oddities (repeated identifiers, rows
where `numberofchildren` exceeds `numberofseats`); the evaluation asks about the columns
literally and makes no claim about what they mean in the real world.
