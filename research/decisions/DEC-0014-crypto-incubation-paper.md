# DEC-0014: Crypto desk, paper trading before the backtest gate ("incubation")

- **Status: ACCEPTED on 2026-10-06.** The owner accepted it by merging the pull request that carries this
  status line, after choosing "paper tournament now" in chat.
- **Amends:** DEC-0012, section "Order of work" only. Every other part of DEC-0012 stands.
- **Date drafted:** 2026-10-06, before any backtest or paper result of any crypto strategy.

## Why

DEC-0012 keeps the kill switch on until gate C1 (the backtest) passes. Two days of recording showed what that
means in practice: 135 cycles, 405 pair evaluations and no signal, so nothing to learn about whether the desk's
plumbing can carry a trade from signal to booked exit. The stocks desk had exactly that kind of fault, hidden
for the same reason, and it was found only when an order was finally allowed.

Paper trading on the desk's own simulator costs nothing and risks nothing. The risk it does carry is to the
evidence: a result seen before the backtest could be read as proof, or used to tune a rule.

## Decision

1. **Incubation.** A registered crypto strategy may trade on the paper simulator before it has passed C1.
   Such trading is called incubation.
2. **Incubation is not evidence.**
   - Every journal row and every published figure of a sleeve in incubation is marked as such.
   - Incubation trades never count toward gate C2. A sleeve's C2 count starts on the day its strategy
     passes C1, on the frozen config that passed, and not before.
   - No parameter, pair or limit of a registered strategy is changed because of an incubation result. A
     change is a new trial in family C with its own record, as DEC-0012 already says.
3. **C1 still decides.** The backtest is run once per frozen config. A strategy that fails C1 stops trading
   on paper unless the owner says, in a record, that it continues as incubation.
4. **Sleeves.** Each strategy trades on its own paper book, with its own limits and its own loss latch
   (ADR 0005). Results are reported per sleeve and are never pooled.
5. **The kill switch** stays a single switch for the desk. The owner removes it, on the host, after a full
   day of clean cycles in which every sleeve has recorded what it would have done.

## Not decided here

- Which strategies are registered: DEC-0015.
- Any live order. The desk holds no venue credentials and that does not change.
