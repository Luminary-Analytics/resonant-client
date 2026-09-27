/* Actual source UI/WS/SQLite/Git; trusted scripted thread writers.
 * While another step holds the project's repository, the Team panel says a
 * writer's result and a combining operation are waiting for it.
 * node tests/swarm_repository_wait.browser.cjs [absolute-path-to-playwright-module]
 */
// The app page needs a one-time launch code (lumi/gui/local_access.py); the
// fixture server mints one per page load.
const fixtureLaunch=async info=>(await (await fetch(info.url+'/__fixture__/launch')).json()).url;
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.argv[2] || 'playwright');

// Another step holding the repository: a separate process takes the same
// cross-process file lock the integration steps take, until told to stop.
function holdRepository(python, workspace) {
    const lock = path.join(workspace, '.git', 'sonn-swarm-integration.lock');
    const code = 'import msvcrt,sys\nf=open(sys.argv[1],"a+b")\nf.seek(0,2)\nif f.tell()==0:\n    f.write(b"0");f.flush()\n'
        + 'f.seek(0)\nmsvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)\nprint("held",flush=True)\nsys.stdin.readline()\n';
    const holder = spawn(python, ['-c', code, lock], {windowsHide:true, stdio:['pipe','pipe','pipe']});
    const held = new Promise((resolve, reject)=>{
        holder.stdout.on('data', chunk=>{ if(String(chunk).includes('held')) resolve(); });
        holder.once('exit', code=>reject(Error('Repository holder exited: '+code)));
    });
    return {held, release: ()=>new Promise(resolve=>{ holder.once('exit', resolve); holder.stdin.end('\n'); })};
}

test('The Team panel says when a writer or an operation waits for the repository', {timeout: 120000}, async () => {
    const output = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-swarm-repository-wait-browser-'));
    const server = spawn(process.env.SWARM_PYTHON || 'python', [path.join(__dirname,'fixtures/swarming_ui_server.py'), output, '--writer', '--hold-first-workers'],
        {cwd:output, windowsHide:true, stdio:['ignore','pipe','pipe']});
    let stdout='', stderr='', info, browser, page, holder;
    server.stdout.on('data', chunk=>{stdout+=chunk;});
    server.stderr.on('data', chunk=>{stderr+=chunk;});
    const exited = new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for (let i=0;i<150;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Fixture server failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture server ready metadata missing: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE
            ? {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE} : {headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:1180,height:900}});
        page.setDefaultTimeout(30000);
        const errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>window.app?.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByText('ollama · fixture-native',{exact:true}).waitFor();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled===true);
        await page.getByLabel('Team objective').fill('Update the independent backend and frontend fixture files');
        await page.getByLabel('Allow scoped file changes').check();
        await page.getByLabel('Writable project folders').fill('src');
        await page.getByLabel('Allowance per writer').fill('4');
        await page.getByLabel('Check name',{exact:true}).fill('combined-files');
        await page.getByLabel('Executable',{exact:true}).fill(info.python);
        await page.getByLabel('Arguments, one per line').fill('verify_changes.py');
        await page.getByLabel('Check timeout in seconds').fill('20');
        for(const [i,part] of ['backend','frontend'].entries()) {
            await page.locator('[data-task-objective]').nth(i).fill(`Update src/${part}.txt`);
            await page.locator('[data-task-roots]').nth(i).fill('src');
            await page.getByLabel('Assignment type').nth(i).selectOption('implement');
            await page.getByLabel('Writable folders for this task').nth(i).fill(`src/${part}.txt`);
            await page.getByLabel('Required check names').nth(i).fill('combined-files');
        }
        await page.getByRole('button',{name:'Start scoped team',exact:true}).click();
        // The first writer is in its first (held) request, its worktree made.
        await page.waitForFunction(()=>app._swarmState?.run?.model_requests?.filter(row=>row.state==='started').length===1
            && !app._swarmPending);

        // Another step takes the repository before the writers finish.
        holder=holdRepository(info.python, info.workspace);
        await holder.held;
        await fetch(info.url+'/__fixture__/release-workers',{method:'POST'});
        await page.waitForFunction(()=>(app._swarmState?.run?.workers||[]).some(row=>row.state==='waiting_for_repository'),
            null,{timeout:60000});
        await page.getByText('Worker 1 · Working',{exact:true}).click();
        await page.getByText('Waiting for another step on this repository · still active').first().waitFor();
        await page.screenshot({path:path.join(output,'writer-waiting-for-repository.png')});
        await holder.release();
        holder=null;
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===2
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending,null,{timeout:60000});

        // A combining operation waits for the repository too, and says so.
        holder=holdRepository(info.python, info.workspace);
        await holder.held;
        await page.locator('.swarm-writer-result input').nth(0).check();
        await page.locator('.swarm-writer-result input').nth(1).check();
        await page.getByRole('button',{name:'Prepare selected changes',exact:true}).click();
        const status=page.locator('[data-swarm="integration-status"]');
        await status.filter({hasText:'Preparing changes: running · waiting for another step on this repository'}).waitFor({timeout:60000});
        await page.screenshot({path:path.join(output,'operation-waiting-for-repository.png')});
        await page.setViewportSize({width:390,height:844});
        await status.scrollIntoViewIfNeeded();
        const dialog=page.getByRole('dialog',{name:'Work together'});
        assert.equal(await dialog.evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        await page.screenshot({path:path.join(output,'operation-waiting-for-repository-compact.png')});
        await page.setViewportSize({width:1180,height:900});
        await holder.release();
        holder=null;
        await page.getByText('Ready for verification',{exact:true}).waitFor({timeout:60000});
        assert.equal((await status.innerText()).includes('waiting for another step'),false);
        assert.deepEqual(errors,[]);
        console.log('Repository-wait browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure.txt'),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,'state.json'),JSON.stringify(await page.evaluate(()=>app._swarmState).catch(()=>({})),null,2));
        }
        console.error('Repository-wait browser failure evidence:',output);
        throw error;
    } finally {
        if(holder) await holder.release().catch(()=>{});
        if(browser) await browser.close();
        await fetch(info?.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        const result=await Promise.race([exited,new Promise(resolve=>setTimeout(()=>resolve(null),10000))]);
        if(!result&&server.exitCode===null)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+stderr);
    }
});
