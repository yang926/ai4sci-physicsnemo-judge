"""Use the configured trusted course for the separate package's test suite."""
import sys

from ai4sci_judge.catalog import ROOT


# Training adapters are an explicit course dependency, not an installed judge
# package. Keep a fresh recursive clone testable without a manual PYTHONPATH.
sys.path.insert(0, str(ROOT))
