/* Machine policy trust in the source app: real Settings and WebSocket turns, scripted inference.
 * node tests/policy_trust.browser.cjs [absolute-path-to-playwright-module]
 * Optional POLICY_PYTHON and POLICY_BROWSER_EXECUTABLE select local runtimes.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

const fixtureLaunch = async info => (await (await fetch(info.url + '/__fixture__/launch')).json()).url;
const evidence = async info => (await fetch(info.url + '/__fixture__/evidence')).json();

async function withFixture(mode, run) {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), `lumi-policy-trust-${mode}-`));
    // The fixture moves its home again before importing lumi; it never starts with the real one.
    const isolated = {...process.env, HOME: output, USERPROFILE: output, LUMI_KEYCHAIN: 'off'};
    delete isolated.LUMI_STATE_HOME;
    delete isolated.LUMI_POLICY_FILE;
    const server = spawn(process.env.POLICY_PYTHON || 'python',
        [path.join(__dirname, 'fixtures/policy_trust_ui_server.py'), output, mode],
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
        browser = await chromium.launch(process.env.POLICY_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.POLICY_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
        const page = await browser.newPage({viewport: {width: 1180, height: 900}});
        page.setDefaultTimeout(20000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);
        await run({page, info, output});
        assert.deepEqual(errors, []);
    } finally {
        if (browser) await browser.close();
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        await new Promise(resolve => setTimeout(resolve, 500));
        if (server.exitCode === null) server.kill();
    }
}

async function openPolicySettings(page) {
    // From the keyboard: Ctrl+, opens Settings, the Privacy & security page holds the policy.
    await page.keyboard.press('Control+Comma');
    const privacy = page.locator('#settings-nav-privacy');
    await privacy.waitFor();
    await privacy.focus();
    await page.keyboard.press('Enter');
}

async function backToSession(page) {
    const back = page.locator('#settings-back');
    await back.focus();
    await page.keyboard.press('Enter');
    await page.locator('#user-input').waitFor({state: 'visible'});
}

test('a policy and a key file a person put in the machine folder are ignored, visibly', {timeout: 120000}, async () => {
    await withFixture('planted', async ({page, info, output}) => {
        await openPolicySettings(page);
        const notes = page.locator('.settings-policy-ignored');
        await notes.first().waitFor();
        const texts = await notes.allInnerTexts();
        const policyNote = texts.find(text => text.includes('policy.json'));
        assert.ok(policyNote, JSON.stringify(texts));
        assert.ok(policyNote.startsWith('Policy file ignored: writable by non-administrators.'), policyNote);
        assert.ok(policyNote.includes(path.join(info.machine, 'policy.json')), policyNote);
        if (process.platform === 'win32') {
            assert.ok(texts.some(text => text.startsWith('Policy signing keys file ignored: not read on Windows.')),
                JSON.stringify(texts));
        }
        assert.equal(await notes.first().getAttribute('role'), 'status');
        // As if the file weren't there: no policy, nothing locked, and nothing blocks a turn.
        await page.getByText('No organization policy is installed on this computer.', {exact: false}).waitFor();
        assert.ok(!(await page.locator('#settings-body').innerText()).includes('Planted Corp'));
        await notes.first().evaluate(node => node.scrollIntoView({block: 'center'}));
        await page.screenshot({path: path.join(output, 'policy-trust-planted-desktop.png')});
        await page.setViewportSize({width: 390, height: 844});
        await notes.first().evaluate(node => node.scrollIntoView({block: 'center'}));
        assert.equal(await notes.first().evaluate(node => node.scrollWidth <= node.clientWidth + 1), true,
            'the note fits a phone width');
        await page.screenshot({path: path.join(output, 'policy-trust-planted-compact.png')});
        await page.setViewportSize({width: 1180, height: 900});
        await backToSession(page);

        await page.locator('#user-input').fill('Hello there');
        await page.keyboard.press('Enter');
        await page.getByText('Noted.', {exact: true}).first().waitFor();
        await page.waitForFunction(() => !window.app.isRunning);
        const seen = await evidence(info);
        assert.ok(seen.requests.length >= 1, JSON.stringify(seen.requests));
        const ignored = seen.audit.filter(record => record.type === 'policy.file_ignored').map(record => record.data);
        assert.ok(ignored.some(item => item.kind === 'policy' && item.path === path.join(info.machine, 'policy.json')),
            JSON.stringify(ignored));
        console.log(JSON.stringify({mode: 'planted', screenshots: output, notes: texts, audit: ignored.length}));
    });
});

test('a policy file Group Policy names that can\'t be read refuses model requests', {timeout: 120000}, async () => {
    await withFixture('unreadable', async ({page, info, output}) => {
        await openPolicySettings(page);
        const alert = page.locator('.editor-error[role="alert"]', {hasText: 'Group Policy names'});
        await alert.waitFor();
        const text = await alert.innerText();
        assert.ok(text.includes(`couldn't be read: ${info.missing}`), text);
        assert.ok(text.endsWith('Lumi won’t send model requests until it’s fixed.'), text);
        await alert.evaluate(node => node.scrollIntoView({block: 'center'}));
        await page.screenshot({path: path.join(output, 'policy-trust-unreadable-settings.png')});
        await backToSession(page);

        await page.locator('#user-input').fill('Hello there');
        await page.keyboard.press('Enter');
        const failure = page.locator('.task-card[data-outcome="failed"] .task-run-detail, .error-block').last();
        await failure.waitFor();
        const failureText = await failure.innerText();
        assert.ok(failureText.includes("couldn't be read") && failureText.includes('Ask your administrator to fix it.'),
            failureText);
        // The refusal comes before any request; give a request that would follow time to show up.
        await page.waitForTimeout(1500);
        const seen = await evidence(info);
        assert.equal(seen.requests.length, 0, 'nothing reaches a model while the policy can\'t be read');
        await page.screenshot({path: path.join(output, 'policy-trust-unreadable-turn.png')});
        console.log(JSON.stringify({mode: 'unreadable', screenshots: output, refusal: failureText.slice(0, 200)}));
    });
});
