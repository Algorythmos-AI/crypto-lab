"""Machine learning for the crypto desk (DEC-0016). Everything in this package runs in the ML environment
(`.venv-ml`, built from `requirements-ml.lock.txt`), never in the trading environment: the trading jobs do not
import it, and reach it only by starting `python -m wt.ml.score` as a separate process with a time limit."""
