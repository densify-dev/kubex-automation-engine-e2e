# GPU E2E Feature

This suite installs a deterministic mock GPU exporter and a local Prometheus instance.

CI now runs this GPU coverage as part of both standard Kubernetes matrix lanes instead of as a separate third suite.

Run it on a GPU-capable host or a Kind setup that exposes the host `/proc` mount used by the exporter.

Run it by itself with:

```bash
GPU_SUITE=true \
GPU_KIND_CONFIG=test/e2e/features/gpu/kind-config.yaml \
./test/e2e/scripts/run-gpu-suite.sh
```
