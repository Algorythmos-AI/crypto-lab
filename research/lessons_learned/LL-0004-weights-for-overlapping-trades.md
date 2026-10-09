# LL-0004: A sample-weight bug hides inside a plausible number

Weighting overlapping trades by how unique their holding period is gave a trade that closed inside its first
bar an empty period and so full weight: 2.4% of the signals carried 34% of all weight, and two model
comparisons were computed on it (EXP-0017, EXP-0018). Nothing looked wrong until the weighted win rate (23%)
was set beside the plain one (34%). Rule: after computing weights, print the effective sample size and the
weight share of the smallest and largest groups before fitting anything.
