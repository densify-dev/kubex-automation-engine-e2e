# QoS change resize example

This example demonstrates an eviction driven by a QoS class change. Apply the
Deployment first so the pod is running with the original QoS class, then apply
the policy to trigger the resize and eviction.

## Apply

1. Deploy the workload:

   ```sh
   kubectl apply -f examples/qos-change-resize/deployment.yaml
   ```

2. Apply the automation strategy and static policy:

   ```sh
   kubectl apply -f examples/qos-change-resize/qos-change-resize.yaml
   ```

## Observe

- Check the pod restarts and QoS class change after the policy applies:

  ```sh
  kubectl get pods -l app=qos-change-demo -o wide
  ```

## Cleanup

```sh
kubectl delete -f examples/qos-change-resize/qos-change-resize.yaml
kubectl delete -f examples/qos-change-resize/deployment.yaml
```
