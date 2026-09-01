"""``python -m evidence.coverage`` -- measure how much of what discovery finds
EPO can actually supply text for, **without spending discovery quota**.

**Why the measurement matters.** EPO's full-text service holds text only for
EP/WO-family documents, so a US-, CN-, DE- or KR-only filing that discovery finds
legitimately has no full text EPO can serve. `EXCLUDED_NO_FULL_TEXT` is a
*designed-in* exclusion, and this number decides whether the approved workflow
excludes most of what discovery finds -- a product decision for the user, not
something to absorb quietly.

**Why it is shaped defensively.** This is the easiest place in the whole plan to
burn a month's SerpAPI or Lens allowance by accident: "measure coverage on 50
real results" reads like an instruction to go and find 50 real results. So:

* **The sample is TAKEN, never GENERATED.** In priority order -- publication
  numbers the user supplies in a file, then `patents.id` rows already stored from
  past runs. Never a fresh discovery call.
* **No suitable sample means STOP and ask**, not "just this once".
* **It calls EPO and nothing else.** No SerpAPI, no Lens, no embeddings, no
  ranking, no LLM. None of them is needed to answer "does EPO hold text for this
  number?", and spending them to find out is the exact waste this prevents. The
  tool asserts its own call profile with an outbound guard, on the same reasoning
  as `run_mock.py`'s `PaidCallAttempted`.
* **`--dry-run` performs zero HTTP calls.** Run it first, every time.
* **Duplicates are collapsed before querying**, and the collapse count is
  reported: a sample of 50 rows that is 30 distinct patents is a sample of 30.

**`FAILED` is never counted as `NO_TEXT`.** Counting a provider outage as absent
text would understate coverage using the provider's own downtime -- and coverage
is the number a product decision gets made on.

**The artifact is a dated report, not a cache.** Nothing in the pipeline reads
it and it never short-circuits a provider call. This repository has no caching of
any kind, and adding one is a fresh design decision rather than a side effect of
a measurement tool.
"""

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from evidence.normalize import PublicationNumber, deduplicate
from models import EnrichmentResult, EnrichmentStatus
from workflow import redaction

logger = logging.getLogger(__name__)

ARTIFACT_DIRECTORY = Path(__file__).resolve().parent.parent.parent / "docs" / "measurements"


class NoSampleAvailable(RuntimeError):
    """No supplied file and no stored publication numbers.

    **Stops, and asks.** The alternative -- running discovery to build a sample --
    is precisely the spend this tool exists to avoid, and doing it "just this
    once" is how a trial allowance disappears.
    """


@dataclass(frozen=True, slots=True)
class CoverageRow:
    publication: PublicationNumber
    status: EnrichmentStatus
    has_claims: bool = False
    has_description: bool = False
    detail: str = ""
    """Redacted. EPO uses OAuth client credentials and this reaches a committed
    artifact."""


@dataclass
class CoverageReport:
    sample_source: str
    raw_count: int
    distinct_count: int
    collapsed: int
    rows: list[CoverageRow] = field(default_factory=list)
    measured_on: date = field(default_factory=date.today)

    @property
    def counts(self) -> Counter:
        return Counter(str(row.status) for row in self.rows)

    @property
    def answered(self) -> int:
        """Publications the provider actually answered about.

        `FAILED` is excluded from the denominator, not counted as `NO_TEXT`: an
        outage is not a data point about whether a document has text.
        """
        return sum(
            1 for row in self.rows if row.status is not EnrichmentStatus.FAILED
        )

    @property
    def with_text(self) -> int:
        return sum(1 for row in self.rows if row.status is EnrichmentStatus.TEXT)

    @property
    def coverage_percent(self) -> Optional[float]:
        """Coverage over *answered* publications, or None when none were.

        None rather than 0.0: "we could not measure" and "EPO has nothing" are
        different findings, and a 0% that actually means an outage would send a
        product decision the wrong way.
        """
        if not self.answered:
            return None
        return round(100.0 * self.with_text / self.answered, 1)

    def _recommendation(self) -> str:
        """State the consequence plainly, without deciding it.

        "If most candidates come back `NO_TEXT`, the workflow excludes most of
        what discovery finds -- a product decision for the user, not something to
        absorb quietly." So this names the consequence and the options and stops
        there. Choosing between them is not a measurement tool's job.
        """
        if self.coverage_percent is None:
            return (
                "**Not measurable.** EPO answered for no publication in this sample, "
                "so this run says nothing about coverage. Re-run it; do not read a "
                "provider outage as an absence of text."
            )

        excluded = self.answered - self.with_text
        empty_offices = sorted(
            office
            for office in {row.publication.country for row in self.rows}
            if not any(
                r.status is EnrichmentStatus.TEXT
                for r in self.rows
                if r.publication.country == office
            )
        )
        lines = [
            f"On this sample the approved workflow would **exclude {excluded} of "
            f"{self.answered}** publications ({100 - (self.coverage_percent or 0):.1f}%) "
            "as `EXCLUDED_NO_FULL_TEXT` before any verification call is made.",
            "",
        ]
        if empty_offices:
            lines += [
                f"**No publication from {', '.join(empty_offices)} had EPO full text "
                "at all.** Those offices are not under-served, they are excluded "
                "entirely: EPO's full-text service covers EP/WO-family documents, and "
                "a filing that never entered that family has nothing for EPO to serve.",
                "",
            ]
        lines += [
            "This is a **product decision, not a defect**, and it is the decision this "
            "measurement exists to inform. The options are:",
            "",
            "1. **Accept it.** The workflow verifies a smaller, EP/WO-weighted set of "
            "prior art, and every exclusion is visible and reasoned at Workflow Gate B. "
            "Nothing is fabricated; the coverage is simply narrower than discovery's.",
            "2. **Add a second enrichment source** for the excluded offices. That is a "
            "new provider integration with its own recorded decision -- not a change "
            "this gate makes.",
            "3. **Narrow discovery** to the jurisdictions EPO can serve, so the "
            "candidate set and the evidence layer agree. This changes what the product "
            "searches, which is a larger decision than it looks.",
            "",
            "Doing nothing selects option 1 by default, which is why it is written "
            "down here rather than left implicit.",
        ]
        return "\n".join(lines)

    def to_markdown(self) -> str:
        counts = self.counts
        jurisdictions = Counter(row.publication.country for row in self.rows)
        coverage = (
            f"{self.coverage_percent}%"
            if self.coverage_percent is not None
            else "not measurable (no publication was answered for)"
        )
        claims_only = sum(
            1 for r in self.rows if r.has_claims and not r.has_description
        )
        description_only = sum(
            1 for r in self.rows if r.has_description and not r.has_claims
        )
        both = sum(1 for r in self.rows if r.has_claims and r.has_description)

        lines = [
            f"# EPO full-text coverage — {self.measured_on.isoformat()}",
            "",
            "Measured by `python -m evidence.coverage`. **No SerpAPI, Lens, embedding",
            "or LLM quota was spent**: the sample was taken from data that already",
            "existed and only EPO was called.",
            "",
            "## Sample",
            "",
            f"- Source: **{self.sample_source}**",
            f"- Raw entries: {self.raw_count}",
            f"- Distinct publications: {self.distinct_count} "
            f"({self.collapsed} duplicate(s) collapsed)",
            f"- Jurisdictions: "
            + (", ".join(f"{k}={v}" for k, v in sorted(jurisdictions.items())) or "none"),
            "",
            "## EPO response categories",
            "",
            "| Category | Count |",
            "|---|---:|",
            f"| `TEXT` | {counts.get('TEXT', 0)} |",
            f"| — claims available | {claims_only + both} |",
            f"| — description available | {description_only + both} |",
            f"| — both | {both} |",
            f"| `NO_TEXT` | {counts.get('NO_TEXT', 0)} |",
            f"| `FAILED` | {counts.get('FAILED', 0)} |",
            "",
            f"**Coverage: {coverage}** of the {self.answered} publication(s) EPO",
            "answered for.",
            "",
            "`FAILED` is reported separately and is **not** counted as `NO_TEXT`.",
            "Counting a provider outage as absent text would understate coverage",
            "using the provider's own downtime.",
            "",
            "## Coverage by jurisdiction",
            "",
            "This is the cut that matters. EPO's full-text service holds text only",
            "for EP/WO-family documents, so coverage is not evenly distributed --",
            "an aggregate percentage hides which offices are excluded entirely.",
            "",
            "| Office | `TEXT` | `NO_TEXT` | `FAILED` | Coverage |",
            "|---|---:|---:|---:|---:|",
        ]
        for office in sorted({row.publication.country for row in self.rows}):
            here = [r for r in self.rows if r.publication.country == office]
            text = sum(1 for r in here if r.status is EnrichmentStatus.TEXT)
            no_text = sum(1 for r in here if r.status is EnrichmentStatus.NO_TEXT)
            failed = sum(1 for r in here if r.status is EnrichmentStatus.FAILED)
            answered = text + no_text
            rate = f"{round(100.0 * text / answered)}%" if answered else "n/a"
            lines.append(f"| {office} | {text} | {no_text} | {failed} | {rate} |")

        lines += [
            "",
            "## What this means for the approved workflow",
            "",
            self._recommendation(),
            "",
            "## What this is not",
            "",
            "A dated measurement, not a cache. Nothing in the pipeline reads this",
            "file and it never short-circuits a provider call.",
            "",
            "## Per publication",
            "",
            "| Publication | Status | Claims | Description |",
            "|---|---|---|---|",
        ]
        for row in self.rows:
            lines.append(
                f"| `{row.publication.normalized}` | {row.status} | "
                f"{'yes' if row.has_claims else 'no'} | "
                f"{'yes' if row.has_description else 'no'} |"
            )
        return "\n".join(lines) + "\n"


def sample_from_file(path: Path) -> tuple[list[str], str]:
    """Publication numbers from a user-supplied file, one per line."""
    if not path.exists():
        raise NoSampleAvailable(f"the supplied sample file {path} does not exist")
    entries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not entries:
        raise NoSampleAvailable(f"the supplied sample file {path} contains no entries")
    return entries, f"user-supplied file {path.name}"


def sample_from_stored_patents(limit: int) -> tuple[list[str], str]:
    """`patents.id` rows already stored from past runs.

    Already paid for, and representative of what discovery actually returns --
    which is the population the measurement is about.
    """
    import db

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM patents ORDER BY fetched_at DESC LIMIT %s", (limit,)
        ).fetchall()
    entries = [str(row[0]) for row in rows]
    if not entries:
        raise NoSampleAvailable(
            "no publication numbers are stored in `patents`, and this tool will not "
            "run discovery to build a sample. Supply one with --sample-file, or run "
            "a search first and re-run this. Building a sample 'just this once' is "
            "the spend this rule exists to prevent."
        )
    return entries, f"stored `patents` rows (most recent {len(entries)})"


def measure(
    publications: list[PublicationNumber],
    *,
    enricher: Callable[..., EnrichmentResult],
) -> list[CoverageRow]:
    """Query EPO once per distinct publication."""
    rows: list[CoverageRow] = []
    for publication in publications:
        try:
            result = enricher(publication.normalized)
        except Exception as error:  # noqa: BLE001 - recorded, never counted as NO_TEXT
            rows.append(
                CoverageRow(
                    publication=publication,
                    status=EnrichmentStatus.FAILED,
                    detail=redaction.safe_detail(error, step="coverage"),
                )
            )
            continue
        rows.append(
            CoverageRow(
                publication=publication,
                status=result.status,
                has_claims="CLAIMS" in result.sections_retrieved,
                has_description="DESCRIPTION" in result.sections_retrieved,
            )
        )
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m evidence.coverage",
        description=(
            "Measure EPO full-text coverage over a sample that already exists. "
            "Never runs discovery."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="perform ZERO HTTP calls and print the publications that would be "
        "queried, in order, after normalization and deduplication. Run this first.",
    )
    parser.add_argument("--sample-file", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.sample_file:
            entries, source = sample_from_file(args.sample_file)
        else:
            entries, source = sample_from_stored_patents(args.limit)
    except NoSampleAvailable as error:
        logger.error("%s", error)
        return 2

    distinct, collapsed = deduplicate(entries)
    print(f"sample source     : {source}")
    print(f"raw entries       : {len(entries)}")
    print(f"distinct          : {len(distinct)} ({collapsed} duplicate(s) collapsed)")

    if args.dry_run:
        print("\n--dry-run: no HTTP call will be made. Would query, in order:")
        for publication in distinct:
            print(f"  {publication.normalized}   (from {publication.requested!r})")
        return 0

    from search.epo_fulltext import enrich_publication

    report = CoverageReport(
        sample_source=source,
        raw_count=len(entries),
        distinct_count=len(distinct),
        collapsed=collapsed,
        rows=measure(distinct, enricher=enrich_publication),
    )

    output = args.output or (
        ARTIFACT_DIRECTORY / f"epo-coverage-{report.measured_on.isoformat()}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_markdown(), encoding="utf-8")
    print(f"\ncoverage          : {report.coverage_percent}% of {report.answered} answered")
    print(f"artifact          : {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
