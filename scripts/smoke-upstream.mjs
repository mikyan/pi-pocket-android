#!/usr/bin/env node
// Exercise the original upstream launcher, HTTP authentication and supervisor.
// This does not submit prompts or make model requests.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { access, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

async function unusedPort() {
    const reservation = createServer();
    reservation.listen(0, '127.0.0.1');
    await once(reservation, 'listening');
    const port = reservation.address().port;
    await new Promise((done, fail) => reservation.close((error) => error ? fail(error) : done()));
    // The upstream CLI does not accept port 0. Authentication below also checks
    // ownership in case another process claims this port after the reservation.
    return port;
}

async function terminateTree(child) {
    if (!child?.pid) return;
    if (process.platform === 'win32') {
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
            windowsHide: true,
            stdio: 'ignore',
        });
        const status = await new Promise((done, fail) => {
            killer.once('error', fail);
            killer.once('exit', (code) => done(code));
        });
        if (status !== 0 && child.exitCode === null && child.signalCode === null) {
            throw new Error('Could not stop the smoke-test process tree.');
        }
    } else {
        // The test creates its own detached process group; no unrelated process
        // is targeted. Kill the group even if its launcher has already exited.
        try { process.kill(-child.pid, 'SIGTERM'); } catch (error) {
            if (error.code !== 'ESRCH') throw error;
        }
        const until = Date.now() + 12_000;
        while (Date.now() < until) {
            try { process.kill(-child.pid, 0); } catch (error) {
                if (error.code === 'ESRCH') return;
                throw error;
            }
            await delay(100);
        }
        try { process.kill(-child.pid, 'SIGKILL'); } catch (error) {
            if (error.code !== 'ESRCH') throw error;
        }
    }
    if (child.exitCode === null && child.signalCode === null) {
        await Promise.race([once(child, 'exit'), delay(5000)]);
    }
}

export async function smokeUpstream(sourceDirectory, { timeoutMs = 90_000, log = () => {} } = {}) {
    assert.ok(sourceDirectory, 'An upstream source directory is required.');
    const source = resolve(sourceDirectory);
    const launcher = join(source, 'bin', 'pi-pocket.js');
    await access(launcher);
    const scratch = await mkdtemp(join(tmpdir(), 'pi-pocket-android-smoke-'));
    const home = join(scratch, 'home');
    const data = join(scratch, 'data');
    const workspace = join(scratch, 'workspace');
    await Promise.all([home, data, workspace].map((path) => mkdir(path, { recursive: true })));
    let child;
    let ownerToken;
    let processError;
    let output = '';
    const deadline = Date.now() + timeoutMs;
    const keepOutput = (bytes) => { output = (output + bytes.toString()).slice(-12_000); };
    const redact = (text) => {
        let safe = String(text).replace(/([?&]token=)[^\s&"'<>]+/gi, '$1[redacted]');
        if (ownerToken) safe = safe.split(ownerToken).join('[redacted]');
        return safe;
    };
    const signals = ['SIGINT', 'SIGTERM'];
    let interrupted = false;
    const interrupt = () => { interrupted = true; };
    signals.forEach((signal) => process.once(signal, interrupt));

    try {
        const port = await unusedPort();
        const base = `http://127.0.0.1:${port}`;
        child = spawn(process.execPath, [launcher,
            '--access', 'local', '--host', '127.0.0.1', '--port', String(port),
            '--data', data, '--cwd', workspace, '-y',
        ], {
            cwd: workspace,
            windowsHide: true,
            detached: process.platform !== 'win32',
            stdio: ['ignore', 'pipe', 'pipe'],
            env: {
                ...process.env,
                HOME: home,
                USERPROFILE: home,
                PI_CODING_AGENT_DIR: join(home, '.pi', 'agent'),
                XDG_CONFIG_HOME: join(home, '.config'),
                XDG_CACHE_HOME: join(home, '.cache'),
                PI_POCKET_DIR: data,
                PI_POCKET_GUARD: 'off',
                PI_POCKET_CACHE_KEEPER: 'off',
                NO_COLOR: '1',
            },
        });
        child.once('error', (error) => { processError = error; });
        child.stdout.on('data', keepOutput);
        child.stderr.on('data', keepOutput);

        function running() {
            if (interrupted) throw new Error('Smoke test interrupted.');
            if (processError) throw processError;
            if (child.exitCode !== null || child.signalCode !== null) {
                throw new Error(`Upstream launcher exited before completion (${child.exitCode ?? child.signalCode}).`);
            }
            if (Date.now() >= deadline) throw new Error(`Smoke test timed out after ${timeoutMs} ms.`);
        }
        async function waitFor(check) {
            while (true) {
                running();
                const result = await check();
                if (result !== undefined && result !== false) return result;
                await delay(150);
            }
        }
        async function request(path, init = {}) {
            running();
            return fetch(base + path, {
                ...init,
                redirect: 'manual',
                signal: AbortSignal.timeout(Math.max(1, Math.min(3000, deadline - Date.now()))),
            });
        }
        const getConfig = async () => JSON.parse(await readFile(join(data, 'config.json'), 'utf8'));
        ownerToken = await waitFor(async () => {
            try {
                const config = await getConfig();
                return typeof config.ownerToken === 'string' && config.ownerToken.length >= 16
                    ? config.ownerToken : undefined;
            } catch (error) {
                if (error.code === 'ENOENT' || error instanceof SyntaxError) return undefined;
                throw error;
            }
        });
        const auth = { authorization: `Bearer ${ownerToken}` };
        const hello = await waitFor(async () => {
            try {
                const response = await request('/api/me', { headers: auth });
                if (response.status !== 200) { await response.arrayBuffer(); return undefined; }
                return await response.json();
            } catch (error) {
                if (error instanceof TypeError || error.name === 'TimeoutError') return undefined;
                throw error;
            }
        });
        assert.equal(hello.user?.role, 'owner', 'Bearer token must authenticate as owner.');
        assert.equal(hello.server?.supervised, true, 'The original launcher must supervise the server.');
        assert.ok(Number.isFinite(hello.server?.startedAt), 'Server must expose its process start timestamp.');
        assert.equal(resolve(hello.server.defaultCwd), workspace, 'Temporary workspace was not respected.');
        assert.equal(resolve(hello.server.home), home, 'Temporary HOME was not respected.');
        log('PASS: original launcher starts with isolated data, HOME and workspace.');

        const anonymous = await request('/api/me');
        assert.equal(anonymous.status, 401, 'Anonymous API access must be refused.');
        await anonymous.arrayBuffer();
        const login = await request(`/login?token=${encodeURIComponent(ownerToken)}`);
        assert.equal(login.status, 303, 'Owner sign-in must redirect.');
        assert.equal(login.headers.get('location'), '/');
        const setCookie = login.headers.get('set-cookie') ?? '';
        assert.match(setCookie, /(?:^|;\s*)HttpOnly(?:;|$)/i, 'Sign-in cookie must be HttpOnly.');
        assert.match(setCookie, /^pocket_auth=/);
        await login.arrayBuffer();
        const cookieHello = await request('/api/me', { headers: { cookie: setCookie.split(';')[0] } });
        assert.equal(cookieHello.status, 200, 'The sign-in cookie must authenticate subsequent requests.');
        await cookieHello.arrayBuffer();
        log('PASS: owner token, browser login and cookie authentication.');

        const page = await request('/');
        assert.equal(page.status, 200);
        assert.match(page.headers.get('content-type') ?? '', /text\/html/i);
        const html = await page.text();
        assert.match(html, /<html\b/i);
        const scripts = [...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)];
        assert.ok(scripts.length > 0, 'Frontend must reference at least one JavaScript entry.');
        for (const [, src] of scripts) {
            const url = new URL(src, base);
            assert.equal(url.origin, base, 'Frontend entry must be served by the local upstream.');
            const script = await request(url.pathname + url.search);
            assert.equal(script.status, 200, 'Frontend JavaScript entry must load.');
            assert.match(script.headers.get('content-type') ?? '', /(?:java|ecma)script/i);
            assert.ok((await script.text()).trim().length > 0, 'Frontend JavaScript must not be empty.');
        }
        const vendor = await request('/vendor/preact.mjs');
        assert.equal(vendor.status, 200, 'Production frontend dependencies must be bundled.');
        assert.match(vendor.headers.get('content-type') ?? '', /(?:java|ecma)script/i);
        await vendor.arrayBuffer();
        log('PASS: original frontend HTML, JavaScript and production vendor module.');

        const restart = await request('/api/restart', {
            method: 'POST', headers: { ...auth, 'x-pocket': '1' },
        });
        assert.equal(restart.status, 200, 'Official restart API must be available.');
        assert.equal((await restart.json()).ok, true);
        const restarted = await waitFor(async () => {
            try {
                const response = await request('/api/me', { headers: auth });
                if (response.status !== 200) { await response.arrayBuffer(); return undefined; }
                const result = await response.json();
                return result.server?.startedAt > hello.server.startedAt ? result : undefined;
            } catch (error) {
                if (error instanceof TypeError || error.name === 'TimeoutError') return undefined;
                throw error;
            }
        });
        assert.equal((await getConfig()).ownerToken, ownerToken, 'Restart must preserve owner identity.');
        assert.equal(restarted.user.id, hello.user.id, 'Restart must preserve the owner user.');
        log('PASS: API restart creates a new server and preserves owner identity.');
        return { version: hello.server.pocketVersion, checks: 4 };
    } catch (error) {
        const diagnostic = redact(output).trim();
        // Do not attach the original error object: an AssertionError can expose
        // compared credential values. The message and logs are redacted here.
        throw new Error(redact(error.message) + (diagnostic ? `\nUpstream diagnostic:\n${diagnostic}` : ''));
    } finally {
        signals.forEach((signal) => process.removeListener(signal, interrupt));
        await terminateTree(child);
        assert.equal(dirname(resolve(scratch)), resolve(tmpdir()), 'Cleanup must stay in the temporary directory.');
        assert.ok(basename(scratch).startsWith('pi-pocket-android-smoke-'), 'Cleanup must target this test only.');
        await rm(scratch, { recursive: true, force: true, maxRetries: 4, retryDelay: 250 });
    }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    const source = process.argv[2];
    if (!source || process.argv.length !== 3) {
        console.error('Usage: node scripts/smoke-upstream.mjs <upstream-source-directory>');
        process.exitCode = 2;
    } else {
        try {
            const result = await smokeUpstream(source, { log: console.log });
            console.log(`PASS: Pi Pocket ${result.version} compatibility smoke (${result.checks} groups). No model requests made.`);
        } catch (error) {
            console.error(`FAIL: ${error.message}`);
            process.exitCode = 1;
        }
    }
}
