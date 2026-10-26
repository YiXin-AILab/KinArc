# Contributing to the frontend

The frontend lives in `web/` and talks directly to the Runtime HTTP/SSE API.
Start with [README.md](README.md) for the product behavior and protocol overview.

## Local development

Use Node.js 22.23.0 and npm 10.9.8. With nvm installed:

```sh
cd web
nvm install
nvm use
npm ci
cp .env.example .env.local
npm run dev
```

Start the Runtime and its required services using the repository instructions first.
The frontend does not substitute mock data when the real backend is unavailable.
`DEV_PROXY_TARGET` selects the development API proxy target. `VITE_API_BASE_URL` selects
the browser-visible API base URL; keep provider credentials out of all `VITE_*` variables.

## Before submitting a change

```sh
npm run format
npm run check
npm audit
```

Add a regression test that fails before fixing a bug. Keep tests beside their module or
component, inject external services, and use synthetic test data without real credentials.
Changes to SSE or concurrent submissions should cover cancellation, replay and stale
responses, not just the successful path. Tests may use fixtures; the application must
continue using the single real-backend path.

CI runs the same format, test and typechecked build commands. Dependency auditing is a
separate job because advisories change independently of source code. Do not bypass failed
checks or use forced dependency upgrades without reviewing their impact.

## Code boundaries

- `api/` owns HTTP mapping, deadlines and authentication headers.
- `sse/` frames text; it does not own network requests or UI state.
- `chat/` projects events and assembles display data without mutating source inputs.
- `composables/` owns asynchronous state, cancellation and resource lifetimes.
- `storage/` validates cached data; local persistence is not server acceptance.
- `components/` owns presentation and user interaction.

Use English JSDoc/TSDoc comments for module responsibilities, public contracts and
non-obvious lifecycle rules. Document parameters, results, errors and side effects where
they matter; update comments when behavior changes. Preserve stable message keys and
model-generated whitespace. Avoid unrelated cleanup and additional state frameworks.

## Deployment check

`npm run build` produces `dist/`, which can be served by a static host. Configure the API
base URL at build time, or route `/v1` to the Runtime on the same origin. A reverse proxy
must allow streaming responses without buffering and accommodate server heartbeats and
connection rotation. `npm run preview` is for checking the build locally, not a production
hosting service.

Before a release, manually exercise real-backend send/stop, refresh/reconnect, queued and
steered inputs, attachments and a narrow viewport. Unit tests use jsdom and do not prove
cross-browser layout or end-to-end deployment correctness.
