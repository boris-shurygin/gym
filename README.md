# Gym

A multi-user workout tracking system built around two complementary workflows:

- **At the gym:** a touch-friendly mobile PWA for following a workout, logging sets with very few taps, and browsing history and charts — including offline.
- **Outside the gym:** an AI agent for planning, interpreting results, and changing training programs via API/MCP.

The **agent defines training strategy and progression rules**; the **backend calculates the actual prescribed loads**. Training logs remain separate from plans and are not rewritten when a program changes.

**Project status:** design with accepted UX and deployment decisions (ADRs). The production PWA/API, GitHub Actions workflows and Yandex Cloud resources have not been implemented.

## Documentation

| Document | Purpose |
| --- | --- |
| [Requirements](docs/requirements.md) | Product scope and quality requirements |
| [Architecture](docs/architecture.md) | System components and data flows |
| [Hosting](docs/hosting.md) | Serverless hosting: Yandex Cloud, Cloud Functions, YDB |
| [CI/CD](docs/ci-cd.md) | Accepted GitHub Actions PR tests, staging/production releases and YDB migrations |
| [Domain model](docs/domain-model.md) | Core concepts, invariants, workout and progression states |
| [Mobile app](docs/mobile-app.md) | UX, offline logging and statistics |
| [Agent integration](docs/agent-integration.md) | MCP, API and agent responsibilities |
| [Open questions](docs/open-questions.md) | Decisions we have not yet made |
| [ADRs](docs/adr/README.md) | Accepted architectural decisions |

See [AGENTS.md](AGENTS.md) for instructions to software development agents.

## Next steps

1. Review the initial documentation and correct assumptions.
2. Design the mobile workout flow and offline synchronization protocol.
3. Specify domain invariants and API contracts.
4. Approve the technology stack before scaffolding implementation code.
