/* DLP in the source app: real WebSocket turns, scripted inference, a fixture policy.
 * node tests/dlp_ui.browser.cjs [absolute-path-to-playwright-module]
 * Optional DLP_PYTHON and DLP_BROWSER_EXECUTABLE select local runtimes.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

const CARD = '4111 1111 1111 1111';
const SSN = '123-45-6789';
const fixtureLaunch = async info => (await (await fetch(info.url + '/__fixture__/launch')).json()).url;
const evidence = async info => (await fetch(info.url + '/__fixture__/evidence')).json();

test('DLP redacts, blocks and shows its rules in the source app', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-dlp-browser-'));
    // The fixture moves its home again before importing lumi; it never starts with the real one.
    const isolated = {...process.env, HOME: output, USERPROFILE: output, LUMI_KEYCHAIN: 'off'};
    delete isolated.LUMI_STATE_HOME;
    const server = spawn(process.env.DLP_PYTHON || 'python', [path.join(__dirname, 'fixtures/dlp_ui_server.py'), output],
        {cwd: output, env: isolated, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '', stderr = '', info, browser;
    server.stdout.on('data', chunk => { stdout += chunk; });
    server.stderr.on('data', chunk => { stderr += chunk; });
    try {
        for (let i = 0; i < 300 && !info; i++) {
            const line = stdout.split(/\r?\n/).find(line => line.startsWith('{"url":'));
            if (line) info = JSON.parse(line);
            else if (server.exitCode !== null) throw Error('Fixture server failed: ' + stderr);
            else await new Promise(resolve => setTimeout(resolve, 100));
        }
        assert.ok(info, 'Fixture server ready metadata missing: ' + stderr);
        browser = await chromium.launch(process.env.DLP_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.DLP_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
        const page = await browser.newPage({viewport: {width: 1180, height: 900}});
        page.setDefaultTimeout(20000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);

        // 1. A redact rule: the turn runs, the model gets the redacted copy, a quiet marker says so.
        await page.locator('#user-input').fill(`Refund card ${CARD} for Project Falcon`);
        await page.keyboard.press('Enter');
        const marker = page.locator('.backend-status-dlp');
        await marker.waitFor();
        assert.equal(await marker.getAttribute('role'), 'status');
        const markerText = await marker.innerText();
        assert.match(markerText, /redacted 1 match before sending \(credit_card\)/);
        assert.ok(!markerText.includes(CARD) && !markerText.includes('4111'), markerText);
        await page.getByText('Noted.', {exact: true}).first().waitFor();
        await page.waitForFunction(() => !window.app.isRunning);
        let seen = await evidence(info);
        const turn = seen.requests.find(request => request.max_tokens !== 32);
        assert.ok(turn, JSON.stringify(seen.requests));
        assert.equal(turn.user_msg, 'Refund card [REDACTED:credit_card] for Project Falcon');
        assert.ok(!JSON.stringify(seen.requests).includes(CARD), 'no request (turn or title) may carry the card');
        const afterFirst = seen.requests.length;
        await page.screenshot({path: path.join(output, 'dlp-redacted-desktop.png')});

        // 2. A block rule: nothing is sent, and the turn says which rule and where, never the number.
        await page.locator('#user-input').fill(`My SSN is ${SSN}`);
        await page.keyboard.press('Enter');
        const failure = page.locator('.task-card[data-outcome="failed"] .task-run-detail, .error-block').last();
        await failure.waitFor();
        const failureText = await failure.innerText();
        assert.match(failureText, /data loss prevention rules blocked this request \(us_ssn in your message\)/);
        assert.ok(!failureText.includes(SSN) && !failureText.includes('6789'), failureText);
        await page.waitForFunction(() => !window.app.isRunning);
        seen = await evidence(info);
        assert.equal(seen.requests.length, afterFirst, 'a blocked turn sends nothing');
        // Sending the same content again (to any model) is refused the same way: no Retry buttons.
        const failed = page.locator('.task-card[data-outcome="failed"]').last();
        assert.equal(await failed.getByRole('button', {name: 'Retry another model'}).count(), 0);
        assert.equal(await failed.getByRole('button', {name: 'Retry', exact: true}).count(), 0);
        await page.screenshot({path: path.join(output, 'dlp-blocked-desktop.png')});

        // 3. Continue goes out, with the blocked message left out.
        const proceed = failed.getByRole('button', {name: 'Continue', exact: true});
        await proceed.focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(count => window.app && !window.app.isRunning
            && document.querySelectorAll('.task-card[data-outcome]').length >= count, 3);
        seen = await evidence(info);
        assert.equal(seen.requests.length, afterFirst + 1);
        const later = JSON.stringify(seen.requests.at(-1));
        assert.ok(!later.includes(SSN) && later.includes('[Withheld: this content was blocked'), later);

        // 4. The audit log records rule, action, kind and count, never the text.
        const findings = seen.audit.filter(record => record.type === 'dlp.finding').map(record => record.data);
        const actions = new Set(findings.map(finding => `${finding.rule}:${finding.action}:${finding.kind}`));
        for (const expected of ['credit_card:redact:prompt', 'falcon-codename:flag:prompt', 'us_ssn:block:prompt']) {
            assert.ok(actions.has(expected), `${expected} missing from ${[...actions]}`);
        }
        const auditText = JSON.stringify(seen.audit);
        for (const secret of [CARD, '4111111111111111', SSN, 'Project Falcon']) assert.ok(!auditText.includes(secret), secret);

        // 5. Settings shows the organization's rules, read-only, from the keyboard.
        await page.keyboard.press('Control+Comma');
        const privacy = page.locator('#settings-nav-privacy');
        await privacy.waitFor();
        await privacy.focus();
        await page.keyboard.press('Enter');
        const row = page.locator('.settings-row', {has: page.getByText('Data loss prevention', {exact: true})});
        await row.waitFor();
        const rowText = await row.innerText();
        for (const expected of ['credit_card', 'redacted before sending', 'us_ssn', 'blocks the request',
            'falcon-codename', 'recorded', 'attachment, prompt', 'Managed by Fixture Corp']) {
            assert.ok(rowText.includes(expected), `${expected} missing from ${rowText}`);
        }
        assert.ok(!rowText.includes('Project Falcon'), 'keywords never reach the page');
        assert.equal(await row.locator('input, select, textarea, button').count(), 0, 'nothing here turns a rule off');
        // Settings re-renders as background data arrives: scroll the current row, don't wait for it to settle.
        await row.evaluate(node => node.scrollIntoView({block: 'center'}));
        await page.screenshot({path: path.join(output, 'dlp-settings-desktop.png')});
        await page.setViewportSize({width: 390, height: 844});
        await row.evaluate(node => node.scrollIntoView({block: 'center'}));
        assert.equal(await row.evaluate(node => node.scrollWidth <= node.clientWidth + 1), true, 'the row fits a phone width');
        await page.screenshot({path: path.join(output, 'dlp-settings-compact.png')});
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({screenshots: output, requests: seen.requests.length, findings: findings.length}));
    } finally {
        if (browser) await browser.close();
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        await new Promise(resolve => setTimeout(resolve, 500));
        if (server.exitCode === null) server.kill();
    }
});
