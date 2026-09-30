/* A new tester's first run in the source app, through the real WebSocket, with a
 * real Ollama backend talking to a loopback stub (tests/fixtures/first_run_ui_server.py):
 *
 *  - a /plan from Auto-edit is refused with the offer to run just that plan in
 *    Full-auto; typing goes on in the message box; the keyboard takes the offer; the
 *    conversation, and a new one, stay in Auto-edit and still ask before commands;
 *  - a model's look-alike notice and card get nothing from the page;
 *  - a message refused for want of a model comes back with its image, reading
 *    "Not sent";
 *  - Settings > Connections' Ollama card: a Test leaves fields being edited and
 *    focus alone and ticks nothing; Save does; OLLAMA_HOST is named when it overrides;
 *  - 375 px in both themes.
 *
 * Not run by CI (no browser there): node tests/first_run.browser.cjs [absolute-path-to-playwright-module]
 * Optional FIRST_RUN_PYTHON and FIRST_RUN_BROWSER_EXECUTABLE select local runtimes.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// A 1x1 PNG, dropped on the message box as a person drops a screenshot.
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
const NOTHING_LISTENS = 'http://127.0.0.1:9';

async function fixture(scenario) {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), `lumi-first-run-${scenario}-`));
    const server = spawn(process.env.FIRST_RUN_PYTHON || 'python', [path.join(__dirname, 'fixtures/first_run_ui_server.py'), output, scenario],
        {cwd: output, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env, PYTHONUNBUFFERED: '1'}});
    const run = {output, server, stdout: '', stderr: '', info: null};
    server.stdout.on('data', chunk => { run.stdout += chunk; });
    server.stderr.on('data', chunk => { run.stderr += chunk; });
    run.exited = new Promise(resolve => server.once('exit', (code, signal) => resolve({code, signal})));
    for (let i = 0; i < 600 && !run.info; i++) {
        const line = run.stdout.split(/\r?\n/).find(item => item.startsWith('{"url":'));
        if (line) run.info = JSON.parse(line);
        else if (server.exitCode !== null) throw Error('Fixture server failed: ' + run.stderr);
        else await sleep(100);
    }
    assert.ok(run.info, 'Fixture server ready metadata missing: ' + run.stderr);
    run.evidence = async () => (await fetch(run.info.url + '/__fixture__/evidence')).json();
    run.delayTags = seconds => fetch(run.info.stub + '/__stub__/delay', {method: 'POST', body: JSON.stringify({seconds})});
    return run;
}

async function launch(run) {
    const browser = await chromium.launch(process.env.FIRST_RUN_BROWSER_EXECUTABLE
        ? {headless: true, executablePath: process.env.FIRST_RUN_BROWSER_EXECUTABLE} : {headless: true, channel: 'msedge'});
    const page = await browser.newPage({viewport: {width: 1280, height: 860}});
    page.setDefaultTimeout(20000);
    const record = {errors: [], sent: [], received: []};
    page.on('pageerror', error => record.errors.push(error.message));
    page.on('websocket', socket => {
        socket.on('framesent', frame => { try { record.sent.push(JSON.parse(frame.payload)); } catch {} });
        socket.on('framereceived', frame => { try { record.received.push(JSON.parse(frame.payload)); } catch {} });
    });
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
    await page.goto((await (await fetch(run.info.url + '/__fixture__/launch')).json()).url);
    await page.waitForFunction(() => window.app && app.ws && app.ws.readyState === 1);
    return {browser, page, record};
}

// Runs `body` against a fixture and a page; on failure, keeps a screenshot and the page's text.
async function withApp(scenario, body) {
    const run = await fixture(scenario);
    let opened = null;
    try {
        opened = await launch(run);
        await body({run, ...opened});
        assert.deepEqual(opened.record.errors, []);
        console.log(`First-run ${scenario} browser evidence:`, run.output);
    } catch (error) {
        if (opened) {
            await opened.page.screenshot({path: path.join(run.output, 'failure.png')}).catch(() => {});
            fs.writeFileSync(path.join(run.output, 'failure.txt'), await opened.page.locator('body').innerText().catch(() => ''));
        }
        console.error('Browser failure evidence:', run.output);
        throw error;
    } finally {
        if (opened) await opened.browser.close();
        await fetch(run.info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        let timer;
        const result = await Promise.race([run.exited, new Promise(resolve => { timer = setTimeout(() => resolve(null), 5000); })]);
        clearTimeout(timer);
        if (!result) run.server.kill();
        fs.writeFileSync(path.join(run.output, 'server.log'), run.stdout + '\n' + run.stderr);
    }
}

// No sideways scroll at this width, and the element inside the viewport.
async function fits(page, selector) {
    return page.evaluate(sel => {
        const box = document.querySelector(sel)?.getBoundingClientRect();
        return {sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
            inside: Boolean(box) && box.width > 0 && box.left >= -0.5 && box.right <= window.innerWidth + 0.5};
    }, selector);
}

async function eachTheme(page, check) {
    for (const theme of ['dark', 'light']) {
        await page.evaluate(name => window.LumiAppearance.setTheme(name), theme);
        await sleep(200);
        await check(theme);
    }
}

// The checklist's first step, "Connect a model": done or not (app.js _renderOnboardingChecklist).
const modelStepDone = page => page.evaluate(() => document.querySelector('.onboarding-steps li')?.classList.contains('done') ?? null);

test('a /plan from Auto-edit runs only once granted, and the conversation stays in Auto-edit', {timeout: 180000}, async () => {
    await withApp('chat', async ({run, page, record}) => {
        await page.waitForFunction(() => app._modelRunning === true, null, {timeout: 30000});
        assert.equal(run.info.first_mode, 'auto-edit');
        assert.equal(await page.locator('#perm-label').textContent(), 'Auto-edit');
        assert.equal((await run.evidence()).permission_mode, 'auto-edit');

        // "/plan …" and Enter, then the person goes on typing. The review saw a space press
        // a switch the notice focused; now focus stays here, so every key lands in the box.
        const composer = page.locator('#user-input');
        await composer.click();
        await page.keyboard.type('/plan Add a counter to the README');
        await page.keyboard.press('Enter');
        await page.keyboard.type(' then add tests', {delay: 60});
        const grant = page.locator('.task-card .full-auto-notice .full-auto-grant');
        await grant.waitFor();
        await page.keyboard.type(' and docs', {delay: 30});
        assert.equal(await composer.inputValue(), ' then add tests and docs');
        assert.equal(await page.evaluate(() => document.activeElement?.id), 'user-input');
        assert.equal(await grant.textContent(), 'Run this plan in Full-auto');
        assert.match(await page.locator('.task-card .full-auto-notice-text').textContent(),
            /This conversation is in Auto-edit, and stays in Auto-edit if you run this plan in Full-auto\./);
        assert.equal(await page.locator('.task-card .full-auto-notice').getAttribute('role'), 'status');
        const plans = () => record.sent.filter(frame => frame.command === 'intent_start');
        assert.deepEqual(plans(), [{command: 'intent_start', text: 'Add a counter to the README'}]);

        // The keyboard path: the notice itself, Tab to its one button, Enter.
        await page.locator('.task-card .full-auto-notice-text').click();
        assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('full-auto-notice')), true);
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Run this plan in Full-auto');
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => document.querySelectorAll('.task-card .full-auto-notice').length === 0);
        for (let i = 0; i < 100 && !record.received.some(event => event.event === 'intent.accepted'); i++) await sleep(100);
        assert.deepEqual(plans().at(-1), {command: 'intent_start', text: 'Add a counter to the README', full_auto: true});
        assert.ok(record.received.some(event => event.event === 'intent.accepted'), 'the plan started');
        // Nothing switched the conversation: no mode change was sent, and it's still Auto-edit.
        assert.equal(record.sent.some(frame => frame.command === 'set_permission_mode'), false);
        assert.equal(await page.locator('#perm-label').textContent(), 'Auto-edit');
        assert.deepEqual((({permission_mode, tier}) => ({permission_mode, tier}))(await run.evidence()),
            {permission_mode: 'auto-edit', tier: 'auto-edit'});

        // So a command in this conversation still asks. Escape denies it.
        await composer.fill('RUN-BASH in this conversation');
        await composer.press('Enter');
        await page.waitForFunction(() => getComputedStyle(document.getElementById('permission-dialog')).display !== 'none');
        await page.keyboard.press('Escape');
        await page.waitForFunction(() => !app.isRunning, null, {timeout: 30000});

        // A model's look-alike notice and card: clicking its button sends nothing, and the
        // page's own notice for that session never goes into the model's card.
        await composer.fill('SHOW-LOOKALIKE');
        await composer.press('Enter');
        await page.locator('#model-lookalike').waitFor();
        await page.waitForFunction(() => !app.isRunning, null, {timeout: 30000});
        const before = record.sent.length;
        await page.locator('#model-lookalike').click();
        await sleep(500);
        assert.deepEqual(record.sent.slice(before), []);
        // What the banner's Resume sends for an interrupted session of that id.
        await page.evaluate(() => app.send({command: 'autonomous_mission_resume', intent_id: 'auto-fake-1', session_id: ''}));
        await page.waitForFunction(() => [...document.querySelectorAll('.full-auto-notice-chat')].some(node => !node.closest('.message-content')));
        assert.equal(await page.locator('.message-content .autonomous-orphan-card .full-auto-notice').count(), 0);
        assert.equal(await page.locator('.full-auto-notice-chat:not(.message-content *) .full-auto-grant').textContent(),
            'Resume this session in Full-auto');

        // 375 px, both themes: the chat's notices wrap inside the screen.
        await page.evaluate(() => app.closePreviewPanel?.());
        await page.setViewportSize({width: 375, height: 812});
        await eachTheme(page, async theme => {
            const layout = await fits(page, '.full-auto-notice-chat:not(.message-content *)');
            assert.deepEqual(layout, {sideways: false, inside: true}, theme);
            await page.screenshot({path: path.join(run.output, `notice-375-${theme}.png`)});
        });
        await page.setViewportSize({width: 1280, height: 860});

        // A new conversation starts in Auto-edit too.
        const first = (await run.evidence()).current_session;
        await page.locator('#new-agent-btn').click();
        await page.locator('.new-session-project-option').first().click();
        await page.waitForFunction(() => !app._newSessionInflight, null, {timeout: 20000});
        await sleep(500);
        assert.equal(await page.locator('#perm-label').textContent(), 'Auto-edit');
        const fresh = await run.evidence();
        assert.equal(fresh.permission_mode, 'auto-edit');
        assert.notEqual(fresh.current_session, first);
    });
});

test('a Test ticks nothing and keeps fields and focus; a refused message comes back with its image; Save ticks', {timeout: 180000}, async () => {
    await withApp('card', async ({run, page, record}) => {
        await page.waitForFunction(() => app._modelRunning === false && document.querySelector('.onboarding-steps'), null, {timeout: 30000});
        assert.equal(await modelStepDone(page), false);

        // Settings > Connections, from the profile menu by keyboard.
        await page.locator('#sidebar-account').focus();
        await page.keyboard.press('Enter');
        await page.locator('#account-connections').click();
        const address = page.locator('#ollama-url');
        await address.waitFor();
        // A slow Test, while the person types an API key further down the page.
        await run.delayTags(2.5);
        await address.fill(run.info.stub);
        await address.press('Enter');
        const key = page.locator('input[data-section="api_keys"][data-key="anthropic"]');
        await key.scrollIntoViewIfNeeded();
        await key.click();
        await page.keyboard.type('sk-ant-first-run', {delay: 40});
        const status = page.locator('[data-ollama-status]');
        await page.waitForFunction(() => /Nothing was saved/.test(document.querySelector('[data-ollama-status]')?.textContent || ''), null, {timeout: 20000});
        await run.delayTags(0);
        assert.match(await status.textContent(), /^Ollama answered at http:\/\/127\.0\.0\.1:\d+ with 1 chat model\. Nothing was saved: choose Save to use this address\.$/);
        assert.equal(await key.inputValue(), 'sk-ant-first-run');
        assert.equal(await page.evaluate(() => document.activeElement?.dataset?.key), 'anthropic');
        assert.equal(await address.inputValue(), run.info.stub);
        assert.equal((await run.evidence()).settings_file.network.ollama_url, NOTHING_LISTENS);
        await key.fill('');

        // The Test saved nothing, so "Connect a model" isn't done.
        await page.locator('#settings-back').click();
        await page.waitForFunction(() => document.querySelector('.onboarding-steps'));
        assert.equal(await modelStepDone(page), false);

        // A message and a dropped screenshot, refused: no model runs yet.
        const composer = page.locator('#user-input');
        await composer.click();
        await composer.fill('Fix the failing chart');
        const dataTransfer = await page.evaluateHandle(png => {
            const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], 'chart.png', {type: 'image/png'}));
            return transfer;
        }, PNG);
        await page.dispatchEvent('.input-wrapper', 'drop', {dataTransfer});
        await page.waitForFunction(() => app.attachedImages.length === 1);
        await composer.press('Enter');
        for (let i = 0; i < 100 && !record.received.some(event => event.event === 'error' && event.refused); i++) await sleep(100);
        await page.waitForFunction(() => !app.isRunning && app.userInput.value === 'Fix the failing chart');
        const refused = await page.evaluate(() => {
            const card = [...document.querySelectorAll('.task-card')].at(-1);
            return {images: app.attachedImages.length, shown: document.querySelectorAll('#attached-images img').length,
                state: card?.querySelector('.task-card-state')?.textContent, label: card?.querySelector('.task-run-label')?.textContent,
                recovery: card?.querySelectorAll('[data-recovery]').length, stop: getComputedStyle(document.getElementById('stop-btn')).display};
        });
        assert.deepEqual(refused, {images: 1, shown: 1, state: 'Not sent', label: 'Not sent', recovery: 0, stop: 'none'});
        const sentWithImage = record.sent.filter(frame => frame.command === 'message');
        assert.equal(sentWithImage.length, 1);
        assert.equal(sentWithImage[0].images.length, 1);
        await composer.fill('');
        await page.evaluate(() => { app.attachedImages = []; app.renderAttachedImages(); });

        // Save, by keyboard: the address, Tab past Test, Enter on Save.
        await page.locator('#sidebar-account').click();
        await page.locator('#account-connections').click();
        await address.waitFor();
        await address.fill(run.info.stub);
        await address.press('Tab');
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement?.id), 'ollama-save');
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => /^Connected to/.test(document.querySelector('[data-ollama-status]')?.textContent || ''), null, {timeout: 30000});
        await page.waitForFunction(() => app._modelRunning === true, null, {timeout: 30000});
        assert.equal(await page.evaluate(() => document.activeElement?.id), 'ollama-save');
        const saved = await run.evidence();
        assert.equal(saved.settings_file.network.ollama_url, run.info.stub);
        assert.equal(saved.backend, 'ollama');

        // 375 px, both themes: the card fits, and Tab shows where focus is.
        await page.setViewportSize({width: 375, height: 812});
        await eachTheme(page, async theme => {
            assert.deepEqual(await fits(page, '[data-ollama-card]'), {sideways: false, inside: true}, theme);
            await address.focus();
            await page.keyboard.press('Tab');
            const focus = await page.evaluate(() => ({id: document.activeElement?.id, outline: getComputedStyle(document.activeElement).outlineStyle}));
            assert.equal(focus.id, 'ollama-test', theme);
            assert.notEqual(focus.outline, 'none', theme);
            await page.screenshot({path: path.join(run.output, `connections-375-${theme}.png`)});
        });
        await page.setViewportSize({width: 1280, height: 860});

        // A saved, working connection: a new conversation's checklist has "Connect a model" done.
        await page.locator('#settings-back').click();
        await page.locator('#new-agent-btn').click();
        await page.locator('.new-session-project-option').first().click();
        await page.waitForFunction(() => !app._newSessionInflight, null, {timeout: 20000});
        await page.waitForFunction(() => document.querySelector('.onboarding-steps li')?.classList.contains('done') === true, null, {timeout: 20000});
    });
});

test('with OLLAMA_HOST set, the Ollama card says it overrides the saved address, and what to do', {timeout: 120000}, async () => {
    await withApp('card-env', async ({run, page}) => {
        await page.waitForFunction(() => app._modelRunning === false);
        await page.locator('#sidebar-account').click();
        await page.locator('#account-connections').click();
        const override = page.locator('[data-ollama-override]');
        await override.waitFor();
        assert.match(await override.textContent(),
            /^OLLAMA_HOST is set to http:\/\/127\.0\.0\.1:9 in Lumi’s environment, so Lumi uses that address instead of the one saved here \(this computer\)\. To use the saved address, remove OLLAMA_HOST from your environment variables, or change it, then restart Lumi\.$/);
        assert.doesNotMatch(await page.locator('#ollama-url-help').textContent(), /from now on/);
        await page.locator('#ollama-url').fill(run.info.stub);
        await page.locator('#ollama-save').click();
        await page.waitForFunction(() => /^Saved /.test(document.querySelector('[data-ollama-status]')?.textContent || ''), null, {timeout: 30000});
        const status = await page.locator('[data-ollama-status]').textContent();
        assert.ok(status.startsWith(`Saved ${run.info.stub}, but Lumi uses http://127.0.0.1:9 from OLLAMA_HOST. `), status);
        const saved = await run.evidence();
        assert.equal(saved.settings_file.network.ollama_url, run.info.stub);
        assert.equal(saved.ollama_url, NOTHING_LISTENS);
        assert.equal(await page.evaluate(() => app._modelRunning), false);
        await page.locator('#settings-back').click();
        await page.waitForFunction(() => document.querySelector('.onboarding-steps'));
        assert.equal(await modelStepDone(page), false);
    });
});
