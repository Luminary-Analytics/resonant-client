/* Actual desktop recovery: killed native host, mTLS service and PostgreSQL.
 * Inference is scripted; no external provider or user state is used. */
const test = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

async function runRecovery(missingAdmission = false) {
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG && process.env.SWARM_MANAGED_PYTHON);
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'sonn-managed-recovery-browser-'));
    const child = spawn(process.env.SWARM_MANAGED_PYTHON,
        [path.join(__dirname, 'fixtures/swarming_managed_ui_server.py'), output, process.env.SONN_GOVERNANCE_TEST_CONFIG, '--recovery', ...(missingAdmission ? ['--missing-admission'] : [])],
        {cwd: output, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '', stderr = '', info, browser, page;
    child.stdout.on('data', value => { stdout += value; });
    child.stderr.on('data', value => { stderr += value; });
    const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({code, signal})));
    const commands = [], errors = [];
    try {
        for (let n = 0; n < 300; n++) {
            const line = stdout.split(/\r?\n/).find(value => value.startsWith('{"url":'));
            if (line) { info = JSON.parse(line); break; }
            if (child.exitCode !== null) throw Error('Managed recovery fixture failed: ' + stderr);
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.ok(info, 'Fixture did not become ready: ' + stderr);
        browser = await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.SWARM_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
        page = await browser.newPage({viewport: {width: 1180, height: 900}});
        page.setDefaultTimeout(20000);
        page.on('pageerror', error => errors.push(error.message));
        page.on('websocket', socket => socket.on('framesent', frame => {
            const message = JSON.parse(frame.payload); if (message.command === 'swarm') commands.push(message);
        }));
        await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await page.goto(info.url);
        await page.waitForFunction(id => window.app?.currentSessionId === id, info.session_id);
        await page.locator('#user-input').fill('Preserve my draft during managed recovery');
        await page.getByRole('button', {name: 'Team', exact: true}).click();
        await page.waitForFunction(() => app._swarmState && !app._swarmPending);
        // The fixture retains a separate personal history entry; opening a new
        // preview lets the user explicitly select the organization identity.
        if (await page.getByRole('button', {name: 'New team', exact: true}).isVisible()) {
            await page.getByRole('button', {name: 'New team', exact: true}).click();
        }
        await page.getByLabel('Execution ownership').selectOption('managed');
        await page.waitForFunction(() => app._swarmState?.execution_mode === 'managed' && !app._swarmPending);
        await page.getByRole('button', {name: 'Take over expired team', exact: true}).click();
        await page.getByRole('heading', {name: 'Organization recovery observations', exact: true}).waitFor();
        assert.equal(await page.getByRole('button', {name: 'Continue reviewed team', exact: true}).isEnabled(), false);
        assert.equal(await page.getByRole('button', {name: 'Stop team', exact: true}).isEnabled(), true);
        await page.getByLabel('Retained record type').selectOption('workers');
        await page.waitForFunction(() => app._swarmState?.run?.managed_recovery?.kind === 'workers' && !app._swarmPending);
        const managed = page.locator('[data-swarm="managed-recovery"]');
        await managed.getByRole('button', {name: 'Derive and report retained observation', exact: true}).focus();
        await page.keyboard.press('Enter');
        if (missingAdmission) {
            await page.waitForFunction(() => app._swarmState?.run?.managed_recovery?.operation?.state === 'finished' && !app._swarmPending);
            assert.equal(await page.getByRole('button', {name: 'Continue reviewed team', exact: true}).isEnabled(), false);
            await managed.getByRole('button', {name: 'Check and fence missing admission', exact: true}).click();
        }
        await page.waitForFunction(() => app._swarmState?.run?.managed_recovery?.worker_cleanup_pending === 0 && !app._swarmPending);
        const request = page.getByRole('form', {name: 'Worker 1 request accounting', exact: true});
        if (!missingAdmission) {
            await request.getByLabel('Observed outcome').selectOption('failed');
            await request.getByLabel('Observation evidence').fill('The fixture killed the original host after its exact request reached the provider boundary. Count one request; no unused allowance is inferred.');
            await request.getByRole('button', {name: 'Record request accounting', exact: true}).click();
            await request.waitFor({state: 'detached'});
        } else assert.equal(await request.count(), 0);
        await page.getByLabel('Retained record type').selectOption('requests');
        await page.waitForFunction(() => app._swarmState?.run?.managed_recovery?.kind === 'requests' && !app._swarmPending);
        if (!missingAdmission) await managed.getByRole('button', {name: 'Derive and report retained observation', exact: true}).click();
        await page.waitForFunction(() => app._swarmState?.run?.managed_recovery?.unknown_request_units === 0 && !app._swarmPending);
        await page.getByRole('button', {name: 'First records', exact: true}).click();
        await page.waitForFunction(() => !app._swarmPending);
        assert.equal(await page.getByRole('button', {name: 'Next records', exact: true}).isEnabled(), false);
        // A real reconnect reopens the persisted selection and observation state.
        await page.evaluate(() => app.ws.close());
        await page.waitForFunction(() => app.ws?.readyState === WebSocket.OPEN && !app._swarmPending);
        await page.getByRole('button', {name: 'Refresh team', exact: true}).click();
        await page.waitForFunction(() => !app._swarmPending);
        assert.equal(await page.getByLabel('Retained record type').inputValue(), 'requests');
        await page.setViewportSize({width: 390, height: 844});
        await managed.scrollIntoViewIfNeeded();
        assert.equal(await page.getByRole('dialog', {name: 'Work together'}).evaluate(node => node.scrollWidth <= node.clientWidth + 1), true);
        await page.screenshot({path: path.join(output, 'managed-recovery-compact.png')});
        await page.getByLabel('Retry: Read fact', {exact: true}).check();
        await page.getByLabel('Recovered worker request allowance').fill('3');
        await page.getByRole('button', {name: 'Continue reviewed team', exact: true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => app._swarmState?.run?.submissions?.length === 1
            && app._swarmState.run.workers.every(row => !row.alive) && !app._swarmPending);
        const evidence = await (await fetch(info.url + '/__fixture__/evidence')).json();
        assert.equal(evidence.backend_requests, 2);
        assert.equal(evidence.remote.runs.length, 2);
        assert.equal(evidence.runs[0].run.run.epoch, 2);
        assert.equal(evidence.runs[0].execution_mode, 'managed');
        assert.equal(evidence.private_configuration_in_model, false);
        assert.ok(commands.filter(row => row.action.startsWith('managed_')).every(row => !('outcome' in row) && !('pid' in row) && !('evidence' in row)));
        assert.equal(commands.some(row => row.action === 'managed_fence_absent'), missingAdmission);
        assert.deepEqual(errors, []);
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#user-input').inputValue(), 'Preserve my draft during managed recovery');
        fs.writeFileSync(path.join(output, 'evidence.json'), JSON.stringify({kind: 'actual-managed-killed-host-browser', commands, evidence}, null, 2));
        console.log('Managed recovery browser evidence:', output);
    } catch (error) {
        if (page) {
            await page.screenshot({path: path.join(output, 'failure.png')}).catch(() => {});
            fs.writeFileSync(path.join(output, 'failure.txt'), await page.locator('body').innerText().catch(() => ''));
        }
        console.error('Managed recovery browser failure evidence:', output);
        throw error;
    } finally {
        if (browser) await browser.close();
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        let timer;
        const result = await Promise.race([exited, new Promise(resolve => { timer = setTimeout(() => resolve(null), 5000); })]);
        clearTimeout(timer); if (!result) child.kill();
        fs.writeFileSync(path.join(output, 'server.log'), stdout + '\n' + stderr);
    }
}

test('Managed restart reconciles retained proof and resumes enforced execution through the browser', {timeout: 120000}, () => runRecovery());
test('Lost uncommitted worker admission requires an explicit server absence fence in the browser', {timeout: 120000}, () => runRecovery(true));
