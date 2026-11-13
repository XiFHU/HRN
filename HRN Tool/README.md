# HRN Tool (simple)

    pip install -r requirements.txt
    python app.py          # opens http://127.0.0.1:8770

Keep `app.py` and `index.html` in the same folder. Leaflet and map tiles load from the internet.

1. **Data & map** - upload crash CSV/XLSX and road GeoJSON/GPKG/zipped SHP; choose lat/lon columns
   (and EPSG if coordinates are projected); crashes are snapped to the nearest road within the snap distance.
2. **Crash type** - pick any crash column and the values to study, and give the group a name (e.g. Severe = K+A+B+C).
   Optional: a second group shown only in the factor table (e.g. KSI = K+A), and a subset restriction.
3. **Factors** - pick the crash type to work on: *All selected types together*, or one type at a time
   (e.g. Rear End, then Broadside). Each choice has its own scan and its own ticked factors.
   The second group (e.g. KSI) is counted among the crashes being analysed (e.g. KSI rear-ends).
   Every crash column and road attribute is scanned and shown as a table:
   Category | Factor | Crash Frequency (split by a column you pick, e.g. K / A / B / C / O) | Total #, % |
   group #, % of group, Prob. | second group #, %, Prob. Tick a row to use it as a factor.
   Conditions are `= value`, and for numbers the best `>= x` / `<= x`. Columns are ranked by *excess*
   (focus crashes above what the overall rate predicts).
   **Prob.** = Poisson P(X <= observed) with mean = crashes meeting the condition x overall group rate,
   i.e. how sure we are the group is over-represented. "Tick rows where Prob. >= 95% and % of group >= 5%"
   selects factors with that rule (either group).
   The left panel can be resized by dragging its right edge (double-click resets it).
4. **High-risk network (systemic)** - follows the FHWA systemic approach used by MnDOT, Iowa DOT and the Boston MPO:
   the crash data chose the risk factors in Step 3; Step 4 applies the **road-attribute** factors to **every segment**
   of the network, whether or not it has crashes. Crash-condition factors (lighting, driver, vehicle ...) cannot be applied
   to a road without crashes, so they are listed as the crash profile and not scored.
   - *Weights*: every road factor has a weight (default 1, 0 = off). Presets: *All 1* (count of factors, MnDOT style)
     and *Mountains* (Both 2, Network / Tot Crash 1, Neither 0, from the binomial test with the Column check settings).
     Within a crash type one value per column counts (the highest weight met); Max Score = the highest weight in each
     column, added up. With several crash types the scores add up.
   - *Cut-off*: Primary = top 5% of segments by score and Secondary = next 10% (Boston MPO), or score >= a % of the
     Max Score. Ties at a cut-off are all included. *Rank within* ranks segments separately in each value of a road
     column (facility type, e.g. functional class).
   - The map colours Primary / Secondary / other segments with factors / no factors; crash history is shown in the
     popup but is not used in the score.
5. **Report** (end of Step 4) - Word and Excel: risk factor tables per crash type (Based On by the binomial test,
   weight, NF, pF, pN, pC, Max Score), the crash profile, the HRN summary (per group when ranked within a facility type)
   and the highest scoring segments; the Excel file lists every segment with its score, tier and rank.
6. **Simple view (Step 3, default)** - *View: one row per column* shows each column once with Total, focus and KSI
   counts and the strongest value for each. Tick *Use* on the columns to screen (*0 = blank* when 0 means not recorded),
   click a column name or *Values ›* to see every value and tick factors. Switch to *one row per value* for the full table.
   Spelling variants (CITY STREET / City Street, extra spaces or punctuation) are merged when files are loaded;
   different wording (e.g. ALCOHOL INVOLVED vs Alcohol) is kept apart.
7. **Screening (Step 3)** - every column is labelled *ordered* (numbers, ranges, yes/no, severity) or *unordered*
   (text categories such as lighting or road system). Tick *Use* on the columns to screen, then:
   - *Correlation matrix*: Spearman rho across road segments, ordered columns only; the only outcome is the crash type
     being studied (crashes / mi).
   - *Cramer's V*: per crash, unordered columns only; the only outcome is the crash type (yes/no). Click a cell for the cross-tab.
   - *Screening results* (above the table) lists every screened column with its rho, V and overlaps; *Column check*
     opens the per-value binomial page, where values can be ticked as factors directly.
   - Click a row name in either matrix to open that column's values (focus and KSI over-representation per value);
     Esc closes the top window first.
8. **Method comparison (Step 3)** - after building the matrices, *Compare with binomial test* (under Screening results)
   runs the binomial test (Network / Tot Crash / Both, as in the Mountains method) on every screened column and lines it up
   with Spearman rho / Cramer's V. Screening flags a column when |rho| or V >= the threshold (default 0.1) and p < alpha;
   the binomial test flags it when at least one value qualifies. Each column is marked Both methods / Spearman / V only /
   Binomial only / Neither. *Download Excel* gives the table plus every qualifying value.
