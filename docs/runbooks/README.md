# Runbooks

| Runbook | For |
|---|---|
| [Crypto desk](crypto-desk.md) | What runs, the owner's controls, every page the desk can send and the first step for each, deploys |
| [Cutover from the monorepo](cutover-from-monorepo.md) | Moving the live desk, with its state, from `trading-lab-research` to this repository; verification; rollback |

The host itself (how it is built, its secrets, its tunnel) belongs to the repository that provisions it,
`trading-lab-research` (`docs/runbooks/gcp-host.md` there). This repository puts only `cryptolab-*` units and
helpers on it and reads its secrets file.
