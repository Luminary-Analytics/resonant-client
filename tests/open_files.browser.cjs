/* Clicking a changed file in the source app: a script is shown in its folder, never run.
 * node tests/open_files.browser.cjs [absolute-path-to-playwright-module]
 * Optional OPEN_FILES_PYTHON and OPEN_FILES_BROWSER_EXECUTABLE select local runtimes.
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
const until = async (check, what) => {
    for (let i = 0; i < 100; i++) {
        const value = await check();
        if (value) return value;
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw Error(`Timed out waiting for ${what}`);
};

test('a changed script is shown in its folder, a document opens', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-open-files-browser-'));
    // The fixture moves its home again before importing lumi; it never starts with the real one.
    const isolated = {...process.env, HOME: output, USERPROFILE: output, LUMI_KEYCHAIN: 'off'};
    delete isolated.LUMI_STATE_HOME;
    const server = spawn(process.env.OPEN_FILES_PYTHON || 'python',
        [path.join(__dirname, 'fixtures/open_files_ui_server.py'), output],
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
        browser = await chromium.launch(process.env.OPEN_FILES_BROWSER_EXECUTABLE
            ? {headless: true, executablePath: process.env.OPEN_FILES_BROWSER_EXECUTABLE}
            : {headless: true, channel: 'msedge'});
        const page = await browser.newPage({viewport: {width: 1180, height: 900}});
        page.setDefaultTimeout(20000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session => window.app?.currentSessionId === session, info.session_id);

        // A turn that writes a script and a document: both show as changed files.
        await page.locator('#user-input').fill('Write the setup script and some notes');
        await page.keyboard.press('Enter');
        const changes = page.locator('.task-change-list').last();
        await changes.waitFor();
        await page.waitForFunction(() => !window.app.isRunning);
        assert.equal((await evidence(info)).script_exists, true);
        await changes.locator('summary').click();
        const script = changes.locator(`.task-change-path[data-file-path$="${info.script}"]`);
        const notes = changes.locator('.task-change-path[data-file-path$="notes.md"]');

        // 1. Clicking the script: it isn't opened; the page names it and its type.
        await script.click();
        const dialog = page.locator('#reveal-program-dialog');
        await dialog.waitFor();
        const text = await dialog.innerText();
        assert.ok(text.includes(`Show ${info.script} in its folder?`), text);
        assert.match(text, /is an? [^.]+\. Opening it would run it, so Lumi doesn’t open it\./);
        assert.equal(await dialog.getByRole('button', {name: 'Show in folder'}).count(), 1);
        await page.screenshot({path: path.join(output, 'open-files-dialog-desktop.png')});
        // Focus starts on Cancel, and Escape closes it without doing anything.
        assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Cancel');
        await page.keyboard.press('Escape');
        await dialog.waitFor({state: 'detached'});
        let seen = await evidence(info);
        assert.deepEqual([seen.opened, seen.revealed, seen.ran], [[], [], '']);

        // 2. Again, from the keyboard: Tab to "Show in folder" and press Enter.
        await script.click();
        await dialog.waitFor();
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Show in folder');
        await page.keyboard.press('Enter');
        await dialog.waitFor({state: 'detached'});
        seen = await until(async () => { const value = await evidence(info); return value.revealed.length && value; },
            'the file to be shown in its folder');
        assert.equal(seen.revealed.length, 1);
        assert.ok(seen.revealed[0].endsWith(info.script) && seen.revealed[0].startsWith(info.project), seen.revealed[0]);
        assert.deepEqual(seen.opened, []);

        // 3. A document opens with its own program.
        await notes.click();
        seen = await until(async () => { const value = await evidence(info); return value.opened.length && value; },
            'the document to open');
        assert.equal(seen.opened.length, 1);
        assert.ok(seen.opened[0].endsWith('notes.md'), seen.opened[0]);
        assert.equal(await dialog.count(), 0);

        // 4. The dialog fits a phone width.
        await page.setViewportSize({width: 390, height: 844});
        await script.scrollIntoViewIfNeeded();
        await script.click();
        await dialog.waitFor();
        const box = await dialog.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
        await page.screenshot({path: path.join(output, 'open-files-dialog-compact.png')});
        await dialog.getByRole('button', {name: 'Cancel'}).click();
        await dialog.waitFor({state: 'detached'});

        // Nothing ran the script at any point.
        await new Promise(resolve => setTimeout(resolve, 1500));
        seen = await evidence(info);
        assert.equal(seen.ran, '');
        assert.equal(seen.revealed.length, 1);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({screenshots: output, opened: seen.opened, revealed: seen.revealed}));
    } catch (error) {
        fs.writeFileSync(path.join(output, 'server.log'), stdout + '\n' + stderr);
        throw error;
    } finally {
        if (browser) await browser.close();
        if (info) await fetch(info.url + '/__fixture__/shutdown', {method: 'POST'}).catch(() => {});
        await new Promise(resolve => setTimeout(resolve, 500));
        if (server.exitCode === null) server.kill();
    }
});
