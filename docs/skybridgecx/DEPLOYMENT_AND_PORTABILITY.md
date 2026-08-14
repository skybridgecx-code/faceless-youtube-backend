# Deployment, Packaging, and Portability (P9)

Progression is: **A** local Python modular runtime plus isolated execution; **B** production modular monolith plus API, Postgres, artifact store, and OTel; **C** distributed workers when justified; **D** enterprise orchestration only for real requirements. Kafka, Redis, Kubernetes, microservices, and a Temporal cluster are forbidden speculative infrastructure unless a measured trigger exists.

During proof the generic core remains `tools/project_agent_runtime/` and YouTube remains `app/workflows/`. Target dependency direction is `SkyBridgeCX Runtime <- YouMo, second project, future projects`, never reversed. Package extraction is forbidden until at least two real project profiles prove portability.
