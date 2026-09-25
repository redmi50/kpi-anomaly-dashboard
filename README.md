# KPI Anomaly Investigation Dashboard

An audit of reported KPI figures in a simulated cloud data warehouse, where every
discrepancy is measured rather than asserted and every root cause is traced back to the
rows behind the number.

A figure on a dashboard is a claim, and this repository treats it as one. Each scenario is
a definition that was run, the definition that should have been run, and the two row sets
that explain the difference between them. The reported figure is quoted, the corrected
figure is computed from the certified definition, and the severity is derived from the
relative size of the gap between the two. Nothing is labelled by hand, and no number in a
finding is written in prose, because a number in prose is a second place for it to be
wrong.

## Why this exists

Most reporting defects are invisible from the query. A window that starts a month late
reads as a window; a negated set membership reads as an exclusion; a temporary view left in
a session reads as the table it is named after. In each case the SQL is what the author
intended to write, and the number it returns is not the number the definition implies. The
gap between the two is where an investigation has to work, and it cannot be worked by
reading the query text.

Five decisions follow from that.

* Severity comes from a measured quantity. The relative discrepancy, the distance between
  the reported figure and the agreed one as a fraction of the agreed one, is mapped
  through bands stated in the code. A scenario that moves the headline by a fraction of a
  percent is reported as low, and one that moves it by a fifth is reported as high, from
  the same rule.
* The agreed definition is the default argument. A scenario is that dataclass with exactly
  one field replaced, and a test asserts the one clause property rather than trusting it.
  A scenario that changed two clauses would produce a number that differs for two reasons
  and would explain neither.
* Every number in a finding is measured at run time. The scenario prose states the
  mechanism and the correction, never the size of the discrepancy, because the size is
  what the audit is for.
* The correction names the schema. A temporary object whose name matches a warehouse table
  takes precedence over it, so an unqualified reference can resolve to something other than
  the table a reviewer is reading. The certified views write `main.fact_order_lines`, which
  makes the correction immune to that defect class instead of spreading it into the fix.
* A clean scenario must come back clean. One scenario is a control that reports the
  agreed definition as its own query, and a test asserts it raises nothing. Without it, ten
  scenarios that fired on everything would look identical to ten that worked.

## Quickstart

```bash
git clone <repository-url>
cd kpi-anomaly-dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# See the warehouse: every table, its grain, and its columns
python -m kpi_audit list-tables

# See the roles and what each one may read, checked against the engine
python -m kpi_audit list-roles

# See the certified views and the corrected figures they hold
python -m kpi_audit list-views

# See every scenario, the definition it ran, and the one clause it departs from
python -m kpi_audit list-scenarios

# Investigate every scenario and write reports/investigation.md and .json
python -m kpi_audit run

# Investigate one scenario, or run the audit under a different role
python -m kpi_audit run --scenarios window_final_day_lost
python -m kpi_audit run --role auditor

# Re-render the markdown from a saved run without recomputing anything
python -m kpi_audit report --investigation reports/investigation.json

# Build the workbook and the model export a BI tool binds to
python -m kpi_audit dashboard --out dashboard

# Run the tests
python -m pytest -q
```

`run` writes two artefacts: `investigation.md` for a reader and `investigation.json` for
anything downstream, which carries the full evidence dictionary for every finding rather
than only the sentence.

`dashboard` writes `kpi_dashboard.xlsx`, seven sheets holding the ledger, the findings, the
three certified views, the access matrix and the warehouse inventory, and a `model/` folder
of one CSV per table and per view for a BI tool to import.

## Project layout

```
kpi-anomaly-dashboard/
  kpi_audit/
    catalogue.py      the tables, their grain, the roles, the certified views, the DDL
    warehouse.py      the seeded generator, and the build
    access.py         sessions, enforced grants, and the read log
    anomalies.py      the KPI definitions, and the scenarios that depart from one clause
    findings.py       the severity weights
    investigation.py  the measurement, the findings, and the report
    report.py         markdown and JSON rendering
    dashboard.py      the workbook and the model export
    __main__.py       command line interface
  tests/
    test_catalogue.py    the schema, the grain sentences, the grants, the view SQL
    test_warehouse.py    the seeded rows, the cutover batch, the reconciliation
    test_access.py       enforcement, the read log, and the refusal
    test_anomalies.py    definitions, the one clause property, the generated SQL
    test_investigation.py the measurements, the control, the severities
    test_report.py       the two report formats and the command line
    test_dashboard.py    the workbook, the model export, and the command line
```

## The warehouse

Everything the rest of the package needs to know lives in `kpi_audit/catalogue.py`, so a
reviewer can disagree with the schema in one file rather than in seven.

| Table | Kind | Rows | Grain |
| --- | --- | --- | --- |
| dim_date | dimension | 181 | One row per calendar date in the period. |
| dim_region | dimension | 4 | One row per sales region. |
| dim_channel | dimension | 4 | One row per sales channel. |
| dim_product | dimension | 8 | One row per sellable product. |
| load_audit | audit | 182 | One row per registered load batch. |
| fact_order_lines | fact | 3958 | One row per order line per load. |

The fact grain is the whole reason the replay anomaly is findable. A row carries a surrogate
key for identification and a separate business key for duplicate detection, and the loader
appends, so a business key can appear twice with a later load timestamp and corrected
figures.

The seed and the sizes the generator uses:

| Constant | Value | What it fixes |
| --- | --- | --- |
| SEED | 20250630 | Every generated row, so any figure here reproduces. |
| PERIOD_START | 2025-01-01 | The first instant of the reporting period. |
| PERIOD_END | 2025-06-30 | The last calendar day of the reporting period. |
| CUTOVER_BATCH | B-MIG-BACKLOG-2025-05-10 | The batch that was delivered at least once. |
| CUTOVER_ORDERS | 140 | Orders the cutover batch carried. |
| STATUS_LAG_RATE | 0.30 | Share of final period orders with no status yet. |
| CALL_CENTRE_RATE | 0.05 | Share of final period orders on a channel with no dimension row. |
| REGION_NULL_RATE | 0.06 | Share of fact rows with no region. |
| DISCREPANCY_BANDS | 0.10 high, 0.02 medium | The relative discrepancy each severity starts at. |
| SEVERITY_WEIGHTS | 3 high, 2 medium, 1 low | What one finding of each severity is worth. |

## The scenarios

Ten scenarios. Nine are the certified definition with exactly one clause replaced, and one
is a control. The changed clause is the name of the dataclass field, which is what makes the
discrepancy attributable to that field rather than to the query as a whole.

| Scenario | Family | Changed | Reported | Corrected | Discrepancy | Severity |
| --- | --- | --- | --- | --- | --- | --- |
| control_certified | control | none | 881,881.07 | 881,881.07 | 0.00 percent | none |
| grain_replayed_batch_counted | grain | dedupe | 946,163.47 | 881,881.07 | 7.29 percent | medium |
| window_first_month_dropped | window | window | 724,951.45 | 881,881.07 | 17.79 percent | high |
| window_final_day_lost | window | window | 877,013.77 | 881,881.07 | 0.55 percent | low |
| filter_unknown_status_dropped | filter | status_rule | 870,233.52 | 881,881.07 | 1.32 percent | low |
| filter_returns_counted | filter | status_rule | 996,243.63 | 881,881.07 | 12.97 percent | high |
| join_unmapped_channel_dropped | join | channel_join | 879,787.32 | 881,881.07 | 0.24 percent | low |
| join_unassigned_region_dropped | join | region_join | 833,318.42 | 881,881.07 | 5.51 percent | medium |
| grain_rows_counted_as_orders | grain | measure | 2,733 | 1,353 | 102.00 percent | high |
| session_shadowed_table | session | qualify | 151,911.02 | 881,881.07 | 82.77 percent | high |

The two order count rows are a count rather than a currency, which is why the grain scenario
stores its own agreed definition: it asks how many orders the period received, so its
correct answer is the certified order count and not the revenue baseline.

## The certified views

The corrected definitions ship as objects in the warehouse rather than as prose, so a report
can bind to a figure that is already right.

| View | Holds |
| --- | --- |
| v_revenue_by_month | Net revenue and distinct orders per calendar month. |
| v_revenue_by_channel | Net revenue per channel, with unmatched rows attributed to UNMAPPED. |
| v_load_reconciliation | Rows registered against rows found, per batch. |

The monthly figures the audit is measured against:

| Month | Orders | Net revenue |
| --- | --- | --- |
| January | 240 | 156,929.62 |
| February | 220 | 145,516.49 |
| March | 214 | 152,694.76 |
| April | 244 | 150,377.93 |
| May | 222 | 150,085.92 |
| June | 213 | 126,276.35 |

Six months, **881,881.07** net revenue and **1,353** distinct orders. Every number in the
scenario table above is measured against those two.

## The access matrix

Each cell below is the answer the engine gave when the role asked for that column, not a
restatement of the policy. A grant that is written in a table and not enforced is not a
grant.

| Role | Columns readable | Purpose |
| --- | --- | --- |
| engineer | 40 of 40 | Owns the pipeline. Reads every table, including the load log. |
| analyst | 33 of 40 | Business reporting. Not the load log, and not list prices. |
| auditor | 40 of 40 | Reads everything, to reconcile a batch against the rows it wrote. |
| contractor | 18 of 40 | Reference data only. No fact rows and no list prices. |

Enforcement is an SQLite authorizer on the connection, so a refused read raises rather than
returning a smaller number. That distinction is the point of the module: a permission that
silently narrows a result is worse than one that fails, because the figure that comes back
looks like an answer.

## Findings

Produced by `python -m kpi_audit run` against the committed seed. The data is generated from
a fixed seed, so these figures reproduce exactly.

| Severity | Scenario | Cause in one line |
| --- | --- | --- |
| high | filter_returns_counted | Returned orders counted as revenue. |
| high | grain_rows_counted_as_orders | Order lines counted as orders. |
| high | session_shadowed_table | A leftover session view shadows the fact table. |
| high | window_first_month_dropped | January missing from the reporting window. |
| medium | grain_replayed_batch_counted | The cutover batch counted twice. |
| medium | join_unassigned_region_dropped | Orders with no region dropped by an inner join. |
| low | filter_unknown_status_dropped | Orders with an unknown status dropped. |
| low | join_unmapped_channel_dropped | Unmapped channel rows dropped by an inner join. |
| low | window_final_day_lost | The final day lost to a closed upper bound. |

Ten scenarios run, 1 raised nothing, 4 high, 2 medium, 3 low, total severity weight 19.

Each finding names the rows behind it, because a figure that moved and a figure that moved
for a reason are different findings. The returned orders scenario counted 3,063 rows where
it should have counted 2,733, wrongly including 330 and excluding none. The January window
counted 2,257 where it should have counted 2,733, wrongly excluding 476. The grain scenario
is the exception that proves the rule: it reads exactly the same rows and aggregates them
with a different measure, so its evidence is the measure rather than the row set, and the
evidence dictionary says so under `changed_clause`.

The session scenario is the one a query text review cannot find. The query it runs is the
agreed definition, character for character, and the number it returns is a single region,
because a temporary view named `fact_order_lines` was left in the session and takes
precedence over the table. The audit finds it by inspecting what the session resolved
against, not by reading the query, and the evidence records the shadowed name alongside the
query.

`control_certified` is the one to read first. It reports the agreed definition, and the
audit raises nothing.

The load reconciliation is where the replay is visible from the other side. One batch of 182
registered loads disagrees with the rows it wrote, and it is the cutover batch: 276 rows
registered against 552 found, a variance of 276. It is the only non-zero variance in the
warehouse, and the batch is large because it carried the migration backlog rather than one
day of trade.

## Adding a scenario

1. Add a `Scenario` to `_scenarios()` in `kpi_audit/anomalies.py`, giving the agreed
   definition and the definition that was run. If the scenario needs a session in a
   particular state, put the statements in `setup`; if it asks a different question, give it
   its own `agreed`.
2. Nothing else has to change. The id becomes available to `run --scenarios` and to every
   parametrised test, and the constructor rejects a scenario that changes more than one
   clause.
3. Run `python -m pytest -q tests/test_anomalies.py`. The one clause test will fail until
   the two definitions differ in exactly one field, which is the point.
4. Run `python -m kpi_audit run --scenarios <id>` and check that the severity follows from
   the measured size rather than from what you expected the size to be.

## Limitations

The warehouse is generated, not captured. It is built to contain the defect classes this
project investigates, and a defect class that is not in it is not covered here. The shipped
set is a window that is wrong, a filter that is wrong, a join that is wrong, a grain that is
wrong, a loader that replayed, and session state that substituted an object. A real reporting
failure outside that list will not be found.

Severity is a function of the size of the discrepancy against the agreed figure. It says how
far the number moved, not what the movement cost, and a small discrepancy in a figure used
to decide something is worth more attention than this ordering implies. The bands are
judgement calls, stated in full so a reader can disagree with the threshold rather than guess
at it: moving the high band from 0.10 to 0.05 would reclassify real findings and nothing in
the suite would notice.

The row level reconciliation is limited to the warehouse. A discrepancy against a downstream
system, such as a finance ledger, would need that system's own figures and is outside this
audit.

Permissions are enforced on one connection at a time through an SQLite authorizer. That is a
real engine check rather than a documented policy, but it is the database's check inside this
process, not a substitute for server side authentication and audit logging.

The dashboard artefact is an Excel workbook and a folder of CSVs, which is what a BI tool
imports. It is not a BI tool, and the workbook does not refresh itself: re-running the
command is what moves a number.
