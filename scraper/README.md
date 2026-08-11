# ArchersHub GE Course Scraper

Pulls GE course offerings from the ArchersHub Course Finder into the
`course_offerings.db` schema. **You** log in by hand in the browser the script
opens — it never sees your password.

> Use gently: it's behind the DLSU login and subject to ArchersHub's terms.
> Keep `--delay` at a second or more and scrape only your own GE list.

## Install (one time)

```bash
pip install playwright
python -m playwright install chromium
```

## 1. Verify selectors on the live page

The site is behind login, so the exact dropdown/table selectors are confirmed
against the real page first:

```bash
python -m scraper.scrape_ge --inspect
```

Log in, open the Course Finder, press ENTER. It prints the `<select>` controls
and the results-table columns. If the column order differs from the defaults in
`scrape_ge.py` (`COL_TEACHER`, `COL_SECTION`, `COL_SCHEDULES`, `COL_REMARK`),
adjust those constants.

## 2. Scrape

Put your GE codes in `scraper/ge_courses.txt` (one per line), then:

```bash
python -m scraper.scrape_ge --courses scraper/ge_courses.txt --out-prefix scraper/out/ge_offerings
```

Writes `scraper/out/ge_offerings.csv` and `.json`. Login persists in
`scraper/.pw-profile/` (gitignored), so later runs usually skip the login.

## 3. Review, then import

Eyeball the CSV, then load it:

```bash
python -m scraper.import_to_db --in scraper/out/ge_offerings.json
# or wipe + re-import (use the ff if re-scraping the same term, so that duplicates dont appear)
python -m scraper.import_to_db --in scraper/out/ge_offerings.json --replace-codes
```

```bash
# sql statements for the paper

# 1. Number of sections offered per course per term
SELECT term, course_code, count(*) FROM course_offerings WHERE term='1261' GROUP BY course_code;
SELECT term, course_code, count(*) FROM course_offerings WHERE term='1241' GROUP BY course_code;

# 2. Number of courses and sections offered per course per term
SELECT TERM, count(DISTINCT course_code) AS courses_offered  count(*) AS sections_offered FROM course_offerings GROUP BY term;
```

## Terms

Every scraped row is stamped with a `term` (default **1261**, the incoming
term) via `--term`. The existing archived data is already tagged **1241**. The
scheduler and SQL agent scope every query to a single term (`SCHEDULE_TERM`,
default `1261`), so the two terms never mix. To run the scheduler/eval against
the old data instead, set `SCHEDULE_TERM=1241`.

## Notes

- **Teacher names** are captured raw (site shows `First Last`; the existing DB
  uses `LAST, FIRST`). Normalising is a deliberate separate step — auto-flipping
  breaks on middle names and suffixes.
- **Days/times** are normalised to DB codes: `THURSDAY→H`, `SATURDAY→S`, 12h→24h.
- **Room**: hybrid courses list an Online slot + a Room slot; the physical room
  is kept and fully-online courses store `NULL`.
- Parsing is covered by `scraper/test_parse.py` (`pytest scraper/test_parse.py`).
