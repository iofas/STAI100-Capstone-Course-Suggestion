"""Scrape GE course offerings from ArchersHub's Course Finder.

You log in by hand in the browser this script opens; the script never sees
your password. It then drives the Course dropdown for each code in your GE
list, scrapes the rendered table, and writes CSV + JSON.

Usage
-----
    # 1) First time: install the browser engine
    #    pip install playwright && python -m playwright install chromium

    # 2) Discover the real dropdown/table selectors on the live page
    python -m scraper.scrape_ge --inspect

    # 3) Full run
    python -m scraper.scrape_ge --courses scraper/ge_courses.txt \
        --out-prefix scraper/out/ge_offerings

The login profile is persisted under scraper/.pw-profile so you usually only
log in once. Be gentle: keep --delay >= 1.0 and scrape only your own GE list.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .parse import Offering, build_offering, course_code_from_option

# --- Site-specific configuration -------------------------------------------
# Everything here is centralised so it can be adjusted after --inspect without
# touching the scraping logic below.
DEFAULT_URL = "https://archershub.dlsu.edu.ph"
DEFAULT_CAMPUS = "Manila"
DEFAULT_SESSION = "AY 2026-2027 Term 1"
DEFAULT_TERM = "1261"  # DLSU term code stamped on every scraped row

# The three Select2 dropdowns (confirmed via --inspect). They're jQuery
# Select2 widgets whose native <select> is hidden, so we set them by value +
# trigger('change') rather than Playwright's select_option/click.
CAMPUS_SELECT = "ddlSelectCampus"
SESSION_SELECT = "ddlSelectAcadSession"
COURSE_SELECT = "ddlSelectCourse"

# The results table (from the page's DOM: <table id="tblCourseSelection">).
TABLE_SELECTOR = "#tblCourseSelection"
ROW_SELECTOR = f"{TABLE_SELECTOR} tbody tr"

# Column order in each row's <td>s (0-indexed), per the Course Finder layout:
# Course Type | Teacher | Credits | Section | Schedules | Cap | Enrolled | Remark | Action
COL_TEACHER = 1
COL_SECTION = 3
COL_SCHEDULES = 4
COL_REMARK = 7

PROFILE_DIR = Path(__file__).with_name(".pw-profile")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def read_courses(path: Path) -> list[str]:
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept either "GEWORLD" or "GEWORLD - THE CONTEMPORARY WORLD".
        codes.append(course_code_from_option(line))
    return codes


# JS run in the page: find the option in a Select2-backed <select> whose text
# matches `needle` (exact, or "<code> - ..." prefix for course codes), set it,
# and fire the change event the way Select2 + the page's AJAX expect.
_SELECT2_JS = r"""
({selId, needle}) => {
  const sel = document.getElementById(selId);
  if (!sel) return {ok: false, reason: 'no <select> #' + selId};
  const want = needle.trim().toLowerCase();
  const opts = Array.from(sel.options);
  const norm = o => o.text.trim().toLowerCase();
  // Exact match first (Campus/Session); else "CODE - NAME" prefix (Course).
  let opt = opts.find(o => norm(o) === want)
         || opts.find(o => norm(o).startsWith(want + ' -'))
         || opts.find(o => norm(o).startsWith(want + ' '));
  if (!opt) return {ok: false, reason: 'no option matching ' + needle};
  sel.value = opt.value;
  if (window.jQuery && window.jQuery(sel).val !== undefined) {
    window.jQuery(sel).val(opt.value).trigger('change');
  } else {
    sel.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return {ok: true, text: opt.text.trim(), value: opt.value};
}
"""


def resolve_target(ctx, timeout_s: int = 30):
    """Find the tab/frame that actually holds the Course Finder controls.

    The portal may open the module in a new tab or inside an iframe, so we
    scan every page and every frame for #ddlSelectCourse rather than assuming
    the first tab. Returns a Page or Frame (both expose the locator/evaluate
    API the rest of the script uses), or None if not found in time.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for pg in list(ctx.pages):
            for fr in list(pg.frames):
                try:
                    if fr.locator(f"#{COURSE_SELECT}").count() > 0:
                        where = "iframe" if fr != pg.main_frame else "page"
                        log(f"Found Course Finder in {where}: {pg.url}")
                        return fr
                except Exception:  # noqa: BLE001  (cross-origin / mid-nav frames)
                    continue
        time.sleep(1)
    return None


def wait_for_options(target, select_id: str, min_count: int = 2,
                     timeout: int = 20000) -> int:
    """Wait until a <select> has been populated (cascading AJAX). Returns the
    final option count (0 on timeout)."""
    try:
        target.wait_for_function(
            "([id, n]) => { const s = document.getElementById(id);"
            " return s && s.options.length >= n; }",
            arg=[select_id, min_count],
            timeout=timeout,
        )
    except PWTimeout:
        pass
    try:
        return target.locator(f"#{select_id} option").count()
    except Exception:  # noqa: BLE001
        return 0


def choose_dropdown(page, select_id: str, needle: str) -> bool:
    """Select the option matching `needle` in the Select2 dropdown `select_id`.

    Sets the hidden native <select> and triggers 'change' via jQuery so both
    Select2's rendered widget and the page's AJAX handlers react. Returns True
    on success.
    """
    try:
        res = page.evaluate(_SELECT2_JS, {"selId": select_id, "needle": needle})
    except Exception as e:  # noqa: BLE001
        log(f"  ! {select_id}: evaluate failed: {e}")
        return False
    if not res or not res.get("ok"):
        log(f"  ! {select_id}: {res.get('reason') if res else 'no result'}")
        return False
    log(f"  set {select_id} -> {res['text']!r}")
    # Let any triggered AJAX settle (ignore if the page long-polls).
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    return True


def scrape_table(page, course_code: str, term: str) -> list[Offering]:
    """Read the currently-rendered results table into Offering rows."""
    offerings: list[Offering] = []
    rows = page.locator(ROW_SELECTOR)
    n = rows.count()
    for i in range(n):
        cells = rows.nth(i).locator("td")
        if cells.count() <= COL_SCHEDULES:
            continue
        teacher = cells.nth(COL_TEACHER).inner_text().strip()
        section = cells.nth(COL_SECTION).inner_text().strip()
        schedules = cells.nth(COL_SCHEDULES).inner_text().strip()
        remark = (
            cells.nth(COL_REMARK).inner_text().strip()
            if cells.count() > COL_REMARK
            else ""
        )
        if not section or not schedules:
            continue
        try:
            offerings.append(
                build_offering(
                    term=term,
                    course_code=course_code,
                    teacher=teacher,
                    section=section,
                    schedules_text=schedules,
                    remark=remark,
                )
            )
        except ValueError as e:
            log(f"  ! skipped row (section {section!r}): {e}")
    return offerings


def inspect(page) -> None:
    """Dump dropdown options and the first table so selectors can be verified."""
    log("\n=== INSPECT: <select> controls on this page ===")
    selects = page.locator("select")
    for i in range(selects.count()):
        sel = selects.nth(i)
        name = sel.get_attribute("id") or sel.get_attribute("name") or f"select#{i}"
        opts = sel.locator("option")
        sample = [opts.nth(j).inner_text().strip() for j in range(min(opts.count(), 6))]
        log(f"[{name}] {opts.count()} options; sample: {sample}")

    log(f"\n=== INSPECT: rows under {ROW_SELECTOR} ===")
    rows = page.locator(ROW_SELECTOR)
    log(f"found {rows.count()} row(s)")
    if rows.count():
        cells = rows.first.locator("td")
        for k in range(cells.count()):
            log(f"  td[{k}] = {cells.nth(k).inner_text().strip()!r}")


def write_outputs(offerings: list[Offering], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    rows = [o.as_dict() for o in offerings]
    fields = list(Offering.__annotations__.keys())

    csv_path = prefix.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    json_path = prefix.with_suffix(".json")
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    log(f"\nWrote {len(rows)} rows:\n  {csv_path}\n  {json_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--campus", default=DEFAULT_CAMPUS)
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--term", default=DEFAULT_TERM, help="term code stamped on scraped rows")
    ap.add_argument("--courses", type=Path, default=Path("scraper/ge_courses.txt"))
    ap.add_argument("--out-prefix", type=Path, default=Path("scraper/out/ge_offerings"))
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between courses")
    ap.add_argument("--inspect", action="store_true", help="dump selectors and exit")
    ap.add_argument("codes", nargs="*",
                    help="course code(s) to scrape; overrides --courses file")
    args = ap.parse_args()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        log("\n" + "=" * 64)
        log("Log in, navigate to the Course Finder page, then return here.")
        input("Press ENTER when the Course Finder table is visible... ")
        log("=" * 64)

        # Locate the tab/frame with the controls (may be a new tab or iframe).
        target = resolve_target(ctx)
        if target is None:
            log(f"! Could not find #{COURSE_SELECT} in any open tab/frame.")
            log("  Make sure the Course Finder page is open, then rerun.")
            ctx.close()
            return 2
        owner = target.page  # the Page that owns the frame, for tab-level waits

        # Set campus, then session. These cascade: campus -> session list,
        # session -> course list (each via AJAX), so we wait between steps.
        if not choose_dropdown(target, CAMPUS_SELECT, args.campus):
            log(f"! Could not set Campus to {args.campus!r} (continuing).")
        owner.wait_for_timeout(500)
        if not choose_dropdown(target, SESSION_SELECT, args.session):
            log(f"! Could not set Session to {args.session!r} (continuing).")

        # The course dropdown is populated by AJAX after Campus+Session are set.
        n_courses = wait_for_options(target, COURSE_SELECT, min_count=2)
        if n_courses < 2:
            log("! Course list did not populate after Campus+Session. The "
                "cascade may need a different trigger, or the term has no "
                "offerings loaded yet.")
        else:
            log(f"Course list populated: {n_courses} option(s).")

        if args.inspect:
            inspect(target)
            ctx.close()
            return 0

        if args.codes:
            codes = [course_code_from_option(c) for c in args.codes]
        else:
            codes = read_courses(args.courses)
        log(f"Scraping {len(codes)} course(s) as term {args.term}: {codes}")

        tbody = target.locator(f"{TABLE_SELECTOR} tbody")

        all_offerings: list[Offering] = []
        for code in codes:
            log(f"\n-> {code}")
            # Snapshot the table so we can tell when the AJAX has replaced it
            # (otherwise we'd scrape the previous course's stale rows).
            prev_html = tbody.inner_html() if tbody.count() else ""
            if not choose_dropdown(target, COURSE_SELECT, code):
                log(f"   ! could not select course {code!r}; skipping.")
                continue
            try:
                target.wait_for_function(
                    "prev => { const tb = document.querySelector('%s tbody');"
                    " return tb && tb.innerHTML !== prev; }" % TABLE_SELECTOR,
                    arg=prev_html,
                    timeout=15000,
                )
            except PWTimeout:
                log(f"   ! table did not refresh for {code!r}; skipping.")
                continue
            owner.wait_for_timeout(400)  # let the table settle
            got = scrape_table(target, code, args.term)
            log(f"   {len(got)} section(s)")
            all_offerings.extend(got)
            time.sleep(args.delay)

        write_outputs(all_offerings, args.out_prefix)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
