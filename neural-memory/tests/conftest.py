"""Suite-wide safety defaults for heavyweight optional model loaders."""

import os


# Unit and contract tests must never start overlapping native Torch/Transformers
# loaders. Dedicated warmup tests override this value explicitly.
os.environ["THREECAN_RERANKER_WARMUP"] = "off"
