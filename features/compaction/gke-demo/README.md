# GKE Compaction Demo

This demo shows:

- compaction workloads moving off a light node
- the light node becoming empty
- GKE scaling the empty node away
- Grafana panels for node count and resource requests via port-forward

Run it with an existing cluster, or let the script create one if needed:

```bash
GKE_PROJECT_ID="<your-project>" \
LETSENCRYPT_EMAIL="<you@example.com>" \
CONTROLLER_IMAGE_REPOSITORY=densify/automation-controller \
CONTROLLER_IMAGE_TAG=1.7.0-beta1 \
./test/e2e/features/compaction/gke-demo/run-gke-compaction-demo.sh
```

Useful follow-up commands:

```bash
kubectl -n monitoring port-forward svc/grafana 3000:3000
kubectl -n monitoring port-forward svc/prometheus 9090:9090
kubectl get pods -n compaction-demo -o wide -w
kubectl get nodes -w
```
