# Complex examples

1) `mixed-proactive-static-deployment.yaml`
   - One Deployment targeted by both a StaticPolicy and a ProactivePolicy, each with its own AutomationStrategy.
   - StaticPolicy sets baseline per-container requests/limits; ProactivePolicy enables recommendation-driven adjustments.

Apply example:

```sh
kubectl apply -f examples/complex/mixed-proactive-static-deployment.yaml
```
