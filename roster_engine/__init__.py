"""A roster-first scheduling engine.

Rule 1 of this shop — one operator mans one machine for a whole shift — is made
true by CONSTRUCTION here, not by a check afterwards: the crew is rostered at the
shift boundary and an operator appears in exactly one machine's roster for that
shift, so a hopping schedule cannot be expressed.

This package has ZERO imports from the app's existing (vendored) scheduling
package. It is a from-scratch rebuild, not a fork; the two engines exist side by
side so their plans can be compared on the same book.

Spec: .superpowers/sdd/2026-08-12-roster-first-scheduler/
"""

# Bumped whenever a change here can move a plan. api._inputs_signature folds this
# in, so applied ranks searched under an older version are correctly flagged stale
# instead of replaying under new semantics behind a green banner.
SCHEDULER_FINGERPRINT = "roster-engine-v1"
