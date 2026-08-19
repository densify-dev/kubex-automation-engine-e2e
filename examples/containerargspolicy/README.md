# ContainerArgsPolicy examples

These examples use `busybox` with a shell sleep command. Argument values are inert process arguments; no GPU, vLLM image, or GPU device is required.

## Admission mutation

`simple.yaml` covers:

- wildcard rules plus named `server` overrides;
- equals-form and split-form updates;
- valueless flags and explicit empty values;
- short options, repeated desired options, and removal;
- Deployment and StatefulSet selection;
- a named rule for a missing container, which is skipped;
- `--gpu-memory-utilization` as a plain argument string, not a GPU workload.

Apply and inspect mutated pod specs:

```sh
kubectl apply -f examples/containerargspolicy/simple.yaml
kubectl get pods -n container-args-admission -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{.spec.containers[*].args}{"\n\n"}{end}'
```

`replaceExistingPods` is `false`, so delete a pod to exercise admission on its replacement:

```sh
kubectl delete pod -n container-args-admission -l app=container-args-admission
kubectl get pods -n container-args-admission -o wide
kubectl delete -f examples/containerargspolicy/simple.yaml
```

## Existing-pod convergence

`replace-existing-pods.yaml` creates its Deployment before its policy. The policy is last in the file so an already-running pod can be evaluated for replacement.

```sh
kubectl apply -f examples/containerargspolicy/replace-existing-pods.yaml
kubectl get pods -n container-args-replacement -l app=container-args-replacement -w
# Ctrl-C after replacement is ready.
kubectl get pods -n container-args-replacement -l app=container-args-replacement \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[0].args}{"\n"}{end}'
kubectl delete -f examples/containerargspolicy/replace-existing-pods.yaml
```

The controller requests replacement through its existing policy-evaluation flow; it does not patch the Deployment template or evict pods directly.

## vLLM-shaped CPU example

`vllm-like.yaml` uses a BusyBox stand-in, so it runs without GPU or vLLM image pulls while keeping realistic vLLM labels and arguments. The policy mutates only the `vllm` container:

- replaces `--port=8000` with `--port=8080`;
- replaces split `--max-model-len 4096` with `8192`;
- replaces `--max-num-seqs=32` with `64`;
- replaces `--served-model-name old-model-name`;
- adds `--enable-prefix-caching`;
- adds `--gpu-memory-utilization 0.8` as an inert split-form argument in this CPU-only example;
- removes `--disable-log-stats`.

```sh
kubectl apply -f examples/containerargspolicy/vllm-like.yaml
kubectl get pods -n container-args-vllm -l app=vllm-args-demo \
  -o jsonpath='{.items[0].spec.containers[0].args}{"\n"}'
kubectl delete -f examples/containerargspolicy/vllm-like.yaml
```
