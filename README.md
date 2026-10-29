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
4. **Final map** - roads coloured by number of factors met; filter by factor count and by crash type.
   If factors were picked per type, choose *Each crash type with its own factors* (each type's crash-based
   factors are counted with that type's crashes only; a road's score adds up over the types) or
   *Same factors for all selected types*.
   Crash-based factors (e.g. driver age >= 21) are met when a road has at least N focus crashes with that
   condition; road-based factors (e.g. speed limit = 45) are met by the road attribute itself.
5. **Report** (end of Step 4) - *Download report (Word)* and *Download Excel*: one risk factor table per crash type
   (Based On, NF, pF, pN, pC, Max Score, laid out like the Mountains HRN tables) plus the highest scoring segments.
   pN = share of network miles with a road factor (road factors only); Based On: Network = pF > pN, Tot Crash = pF > pC,
   Both = both; points Both = 2, Network / Tot Crash = 1; Max Score = best points per category, added up.
   The Excel file also lists every segment's score and a score summary. The Word report needs python-docx
   (included in requirements.txt).
