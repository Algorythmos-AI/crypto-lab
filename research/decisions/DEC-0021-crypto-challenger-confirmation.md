# DEC-0021: Crypto desk, a challenger is confirmed on earlier history before it is admitted (amends DEC-0016)

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat a plan that names this step.
- **Amends:** DEC-0016, section 5 (admission). Every other part stands.
- **Date drafted:** 2026-10-08, before any challenger has been registered on the host.

## Why

Up to sixty challengers are tested on the same two years of history. The deflated Sharpe ratio raises the
bar as the count grows, but it is still one stretch of market looked at sixty times, and a rule fitted to it
by chance looks the same as a rule that works. The second exchange serves hourly history for the two years
before that span as well. Nothing has been tested on it: it was set aside for EXP-0021's confirmation, which
no sleeve reached.

## Decision

1. **The confirmation span** is the two years immediately before the span of the registered sleeves' gate C1
   experiment, for the traded pairs that have history there.
2. **Only a challenger that has passed gate C1 is run on it, once.** A challenger that fails C1 never touches
   it. The result is recorded with the challenger's C1 result and is never recomputed.
3. **The run** is the gate's own: the desk's code, the same costs, the challenger alone on its own book. No
   stress run and no random-entry control: those belong to C1.
4. **A challenger is confirmed** when, on that span, it closed at least 15 trades, its mean R after costs is
   above zero and its profit factor is above 1. This is a check that the result has the same sign on other
   data, not a second significance test: C1 has already asked for significance.
5. **A challenger that is not confirmed is not admitted.** It is recorded as failed, with
   `not_confirmed_on_earlier_history` among the reasons, and it never trades. It still counts as a trial.
6. **If the earlier history cannot be loaded,** the challenger stays registered and is judged by a later run.
   It is never admitted unconfirmed.

Nothing here makes admission easier, and nothing changes for the three registered sleeves.

## Not decided here

- The search space, the caps, the draw, retirement, or gate C1 itself.
- Any model, input or setting (DEC-0018, item 7).
