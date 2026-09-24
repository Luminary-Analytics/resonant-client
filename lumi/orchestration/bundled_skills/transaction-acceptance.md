---
name: Transaction and API acceptance
description: Check database APIs for rollback, validation recovery, concurrent mutations and durable state.
triggers: [sqlite, database, transactions, inventory, orders, API, concurrent]
pinned: false
---
Use when HTTP requests mutate shared persistent state.

Test the public interface, independently of implementation helpers. Include invalid-then-valid request sequences on the same running server: duplicate keys, malformed input, missing entities and insufficient inventory must not poison later transactions. Assert response, database totals and history together.

Test competing requests for the last unit and a small concurrent burst. Assert accepted operations match durable records and remaining stock; reject excess demand without negative stock, partial writes or server errors. Repeat the race when its outcome is nondeterministic. Inspect connection ownership and transaction boundaries; one successful sequential request proves neither thread safety nor rollback.

Restart the server against the same temporary database and check persistence. Use a fresh database per test fixture, explicit cleanup, and check_run with a named requirement. Do not modify expectations merely to make a broken implementation pass. Report which conditions were checked and any coverage gaps.
