# Compaction E2E Feature

Compaction coverage uses the shared pytest suite and one live-cluster entry point:

```bash
./test/e2e/run-gke-suite.sh \
  tests/test_compaction_scheduler.py \
  tests/test_compaction_scale.py \
  tests/test_compaction_eviction_loop.py
```

The destructive GKE upgrade lane remains opt-in through `tests/test_compaction_upgrade.py`.
