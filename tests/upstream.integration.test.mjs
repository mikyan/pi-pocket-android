import test from 'node:test';
import { smokeUpstream } from '../scripts/smoke-upstream.mjs';

// Explicit opt-in because this launches the supplied real upstream checkout.
// Example (PowerShell):
// $env:PI_POCKET_TEST_UPSTREAM = (Resolve-Path .cache/upstream).Path
// node --test tests/upstream.integration.test.mjs
test('pinned upstream CLI, frontend, authentication and restart contract', {
    skip: !process.env.PI_POCKET_TEST_UPSTREAM,
    timeout: 120_000,
}, async () => {
    await smokeUpstream(process.env.PI_POCKET_TEST_UPSTREAM);
});
