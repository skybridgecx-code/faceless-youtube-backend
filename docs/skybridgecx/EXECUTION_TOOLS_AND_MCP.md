# Execution, Tools, and MCP (P5)

`ExecutionEnvironment` target interface is `prepare`, `execute`, `snapshot`, `cleanup`. Implementations MAY include `IsolatedGitClone`, `ProcessSandbox`, `ContainerSandbox`, `BrowserWorker`, and `RemoteWorker`. The environment is the blast-radius boundary; coding writers require independent physical workspaces and Git metadata. Filesystem policy MUST evaluate resolved canonical paths including symlinks/traversal; network is deny-by-default.

Credentials use scoped, expiring capability handles/JIT grants, never an ambient model-readable secret dump. Prompts, traces, artifacts, errors, and tool results MUST redact credentials. A `ToolSpec` records namespace/name/version, schemas, risk, capabilities, idempotency, timeout, retry class, concurrency limit, result-size limit, and provider. Tool lists MUST be deterministically filtered before model exposure.

MCP is an adapter/interoperability boundary. Internal domain types MUST remain MCP-independent; remote metadata is untrusted. Long-running remote work exposes durable task identity, not hidden session ownership.
