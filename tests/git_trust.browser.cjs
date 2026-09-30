/* A repository whose own Git settings run a program: no Git until the person trusts the project.
 * node tests/git_trust.browser.cjs [absolute-path-to-playwright-module]
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
const NOTICE = 'Git features are off for this project until you trust it';

test('Git waits for trust in a repository whose settings run a program', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-git-trust-browser-'));
    // The fixture moves its home again before importing lumi; it never starts with the real one.
    const isolated = {...process.env, HOME: output, USERPROFILE: output, LUMI_KEYCHAIN: 'off'};
    delete isolated.LUMI_STATE_HOME;
    const server = spawn(process.env.OPEN_FILES_PYTHON || 'python',
        [path.join(__dirname, 'fixtures/open_files_ui_server.py'), output, 'git'],
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

        // 1. Opening the project asks for its Git status: the banner says why there is none, and stays.
        const banner = page.locator('.runtime-banner-git');
        await banner.waitFor();
        const bannerText = await banner.innerText();
        assert.ok(bannerText.includes(NOTICE) && bannerText.includes('filter.review.clean'), bannerText);
        const trustButton = banner.getByRole('button', {name: 'Trust this project'});
        assert.equal(await trustButton.count(), 1);
        await page.screenshot({path: path.join(output, 'git-trust-banner.png')});

        // 2. The Git popover, from the command palette, keeps the notice.
        await page.keyboard.press('Control+k');
        await page.locator('#cmd-palette-input').fill('Git changes');
        await page.keyboard.press('Enter');
        const popover = page.locator('.git-popover');
        await popover.waitFor();
        assert.ok((await popover.innerText()).includes(NOTICE));
        await page.screenshot({path: path.join(output, 'git-trust-refused.png')});
        await popover.locator('.git-popover-close').click();
        assert.equal((await evidence(info)).git_ran, '', 'no Git ran in the untrusted project');

        // 3. Settings > Privacy & security > Project trust says Git waits too.
        await page.keyboard.press('Control+Comma');
        const privacy = page.locator('#settings-nav-privacy');
        await privacy.waitFor();
        await privacy.click();
        const trust = page.locator('[data-trust-decision="trusted"]');
        await trust.waitFor();
        assert.ok((await page.locator('.settings-section', {has: trust}).innerText()).includes('Lumi’s own Git features'));
        assert.equal((await evidence(info)).git_ran, '', 'still no Git');

        // 4. Trusting from the banner, from the keyboard, brings Git back and the banner line goes.
        await page.keyboard.press('Alt+1');
        await trustButton.waitFor();
        await trustButton.focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => window.app.gitData && !window.app.gitData.refused && window.app.gitData.is_repo);
        await banner.waitFor({state: 'detached'});
        const data = await page.evaluate(() => window.app.gitData);
        assert.deepEqual(data.changes.map(change => change.file), ['data.bin']);
        // Trusted: the repository's filter runs when Git looks at the file, as the person's own `git status` would.
        assert.match((await evidence(info)).git_ran, /clean/);
        assert.deepEqual(errors, []);
        console.log(JSON.stringify({screenshots: output, changes: data.changes}));
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
