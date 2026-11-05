# Reproducing a retained report

A bundle contains the exact input evidence, source files, dependency lock and
runtime identity required by its retained report. Verify the bundle hashes before
using its files. Install the recorded Python version and hash-checked wheels in
an isolated environment. Recompute from the retained inputs and compare canonical
output, units and status. Replay verifies stored bytes; recomputation additionally
checks the recorded calculation implementation.

A missing executable environment yields an explicit unavailable result. It never
silently substitutes current code or a later source revision. Source-use checks
apply before export. General workload capacity and source authenticity are separate
from numerical reproducibility.
