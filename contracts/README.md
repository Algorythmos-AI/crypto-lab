# The snapshot contract

The desk publishes one signed snapshot of itself every 15 minutes. The status dashboard that receives it lives
in another repository; these files are everything the two agree on.

| File | What it is | Made by |
|---|---|---|
| `crypto.schema.json` | JSON Schema (draft-07) of the snapshot. `$id` is `trading-lab/crypto-snapshot`: the dashboard files a snapshot by its id, so the id did not change when the desk moved here. Unknown properties are rejected everywhere | `wt.crypto.snapshot.to_schema()`, from the allowlist `ALLOW` |
| `crypto.v1.json` | A snapshot built by the real builder from a synthetic, seeded desk state. No real prices, trades or balances | `scripts/gen_crypto_fixture.py` |
| `SHA256SUMS` | The sha256 of both files (`shasum -a 256 -c SHA256SUMS`) | `python -m wt.ops.contract` |

`make contracts` regenerates all three; CI fails when they are stale.

## For the consumer

Vendor `crypto.schema.json` and `crypto.v1.json` from a tagged commit of this repository, record the tag and
the two hashes from `SHA256SUMS` in a lock file, and check the hashes in CI. Do not edit the vendored copies.

A contract is tagged by what it is: `contract-<first 12 hex digits of the schema's sha256>`. A schema that
changes gets a new tag; an old tag never moves.

## Changing the contract

The dashboard's ingest rejects any field the schema does not list. So the order is always:

1. change the allowlist here, run `make contracts`, merge, tag;
2. the dashboard vendors the new files and is deployed;
3. only then is this repository deployed. The deploy refuses a commit whose schema differs from the live
   checkout's unless the owner names the new schema's hash (`WT_DEPLOY_CONTRACT=<sha256>`), which the owner
   reads from the dashboard's lock file (`wt.ops.contract`).

Adding an optional field keeps `schema_version` at 1: every older snapshot is still valid. Removing or
retyping a field is a new version and a new fixture file name.
