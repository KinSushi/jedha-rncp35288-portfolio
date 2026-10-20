"""Integration tests: several layers exercised together, without a database.

WHY THESE EXIST SEPARATELY
--------------------------
The unit suite proves each module behaves in isolation. It cannot catch a
contract break *between* modules — a column renamed in cleaning that the feature
builder still expects, a cleaning rule that removes what a control is meant to
judge. Those are the failures that reach production.

WHY THEY DO NOT REQUIRE POSTGRES
--------------------------------
Everything up to the load is file-based and deterministic. Testing that chain
without a database keeps the suite runnable in CI, on a laptop and in a review,
which is the difference between tests that run and tests that exist.

The database-dependent half is covered by `scripts/smoke_test.sh`, which is
honest about needing a live stack.
"""
