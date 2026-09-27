/* Actual source GUI/WebSocket and native workers with a delayed planner reply.
 * Only inference and response delivery timing are controlled; no paid provider. */
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

for(const disconnect of [false,true]) test(disconnect
    ? 'Disconnect before the Stop refresh preserves uncertainty without automatic replay'
    : 'Stop supersedes a delayed follow-up reply and retains captured ownership after panel close', {timeout:90000}, async () => {
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-followup-stop-'));
    const server=spawn(process.env.SWARM_PYTHON || 'python',[path.join(__dirname,'fixtures/swarming_ui_server.py'),output,'--followup-planning'],
        {cwd:output,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',info,browser,page;
    server.stdout.on('data',data=>stdout+=data);
    server.stderr.on('data',data=>stderr+=data);
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    try {
        for(let i=0;i<150;i++) {
            const line=stdout.split(/\r?\n/).find(line=>line.startsWith('{"url":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null) throw Error(stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Fixture ready metadata missing: '+stderr);
        browser=await chromium.launch({headless:true,channel:'msedge'});
        page=await browser.newPage({viewport:{width:390,height:844}});
        page.setDefaultTimeout(15000);
        const errors=[],commands=[];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('websocket',socket=>socket.on('framesent',frame=>{
            const data=JSON.parse(frame.payload);
            if(data.command==='swarm') commands.push(data);
        }));
        await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        await page.goto(await fixtureLaunch(info));
        await page.waitForFunction(session=>app.currentSessionId===session,info.session_id);
        await page.getByRole('button',{name:'Team',exact:true}).click();
        await page.getByLabel('Enable team preview').check();
        await page.waitForFunction(()=>app._swarmState?.enabled && !app._swarmPending);
        await page.getByLabel('Team objective').fill('Stop during a delayed follow-up reply');
        await page.locator('[data-swarm="plan-mode"]').selectOption('coordinator');
        await page.getByLabel('Total model requests').fill('20');
        await page.getByLabel('Coordinator request allowance',{exact:true}).fill('2');
        await page.getByLabel('Allowance per worker',{exact:true}).fill('4');
        await page.getByRole('button',{name:'Request a proposed plan',exact:true}).click();
        await page.getByText('Coordinator · Complete',{exact:true}).waitFor();
        await page.getByLabel('Plan review notes').fill('Approve two scoped investigations for this Stop fixture.');
        await page.getByRole('button',{name:'Approve plan',exact:true}).click();
        await page.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1
            && app._swarmState.run.attempts.some(row=>row.kind==='worker' && row.process_state==='running')
            && app._swarmState.coordinator_planning?.available && !app._swarmPending);
        const captured=await page.evaluate(()=>({scope:app._swarmScope,
            peer:app._swarmState.run.attempts.find(row=>row.kind==='worker' && row.process_state==='running').id}));
        // Hold the actual service reply, then also hold the fresh view requested
        // by Stop. No fabricated success response or direct service mutation.
        await page.evaluate(()=>{
            window.delayedSwarm={original:app.receiveSwarmState.bind(app),planner:null,refresh:null};
            app.receiveSwarmState=function(event){
                if(this._swarmPending?.action==='request_plan' && event.request_id===this._swarmPending.request_id){
                    delayedSwarm.planner=event;return;
                }
                if(this._swarmStopRefresh?.request_id===event.request_id){delayedSwarm.refresh=event;return;}
                delayedSwarm.original(event);
            };
        });
        await page.getByLabel('Follow-up coordinator allowance',{exact:true}).fill('2');
        await page.getByLabel('Follow-up readable folders',{exact:true}).fill('');
        await page.getByRole('button',{name:'Request follow-up plan',exact:true}).click();
        await page.waitForFunction(()=>delayedSwarm.planner!==null);
        const stop=page.getByRole('button',{name:'Stop team',exact:true});
        assert.equal(await stop.isEnabled(),true,'Stop must remain available behind the delayed planner reply');
        await stop.focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(()=>delayedSwarm.refresh!==null);
        assert.equal(commands.filter(row=>row.action==='stop').length,0,'No stale-revision Stop before fresh view');
        if(disconnect) {
            await page.evaluate(()=>app.ws.close());
            await page.waitForFunction(()=>!app._swarmStopRefresh);
            await page.evaluate(()=>{delayedSwarm.original(delayedSwarm.refresh);delayedSwarm.original(delayedSwarm.planner);});
            await page.waitForFunction(()=>app.ws?.readyState===WebSocket.OPEN && !app._swarmPending
                && app._swarmState.run.attempts.filter(row=>row.kind==='coordinator').every(row=>row.process_state==='stopped'));
            assert.equal(commands.filter(row=>row.action==='stop').length,0,'A lost read never automatically replays Stop');
            assert.equal(await page.evaluate(()=>app._swarmState.run.run.stop_requested),0);
            await page.getByRole('button',{name:'Stop team',exact:true}).click();
        } else {
            // Closing a panel cannot redirect or cancel the already requested Stop.
            await page.getByRole('button',{name:'Close team panel',exact:true}).click();
            await page.waitForFunction(()=>!app._swarmDialog);
            await page.evaluate(()=>delayedSwarm.original(delayedSwarm.refresh));
            await page.waitForFunction(()=>!app._swarmStopRefresh);
        }
        for(let i=0;i<100 && !commands.some(row=>row.action==='stop');i++) await page.waitForTimeout(20);
        const sent=commands.filter(row=>row.action==='stop');
        assert.equal(sent.length,1);
        assert.equal(sent[0].run_id,captured.scope.run_id);
        assert.equal(sent[0].session_id,captured.scope.session_id);
        assert.equal(sent[0].project,captured.scope.project);
        if(!disconnect) {
            assert.equal(sent[0].expected_revision,await page.evaluate(()=>delayedSwarm.refresh.run.run.revision));
            await page.getByRole('button',{name:'Team',exact:true}).click();
        }
        // This source fixture intentionally holds a synchronous provider call.
        // Stop must show pending cleanup until the fixture releases that call;
        // this is separate from production child-process termination evidence.
        await page.waitForFunction(()=>app._swarmState?.run?.run.stop_requested && !app._swarmPending);
        assert.equal(await page.evaluate(()=>app._swarmState.run.run.state),'stopping');
        await fetch(info.url+'/__fixture__/release-workers',{method:'POST'});
        await page.waitForFunction(()=>app._swarmState?.run?.run.stop_requested
            && app._swarmState.run.attempts.every(row=>row.process_state==='stopped') && !app._swarmPending);
        const stopped=await page.evaluate(()=>app._swarmState.run);
        assert.equal(stopped.run.id,captured.scope.run_id);
        assert.equal(stopped.attempts.find(row=>row.id===captured.peer).process_state,'stopped');
        assert.equal(stopped.attempts.filter(row=>row.kind==='coordinator').length,2);
        assert.ok(stopped.attempts.filter(row=>row.kind==='coordinator').every(row=>row.process_state==='stopped'));
        const before=await page.evaluate(()=>({run:app._swarmState.run.run,scope:app._swarmScope}));
        await page.evaluate(()=>delayedSwarm.original(delayedSwarm.planner));
        assert.deepEqual(await page.evaluate(()=>({run:app._swarmState.run.run,scope:app._swarmScope})),before,
            'Late planner reply cannot replace stopped retained state');
        assert.equal(commands.filter(row=>row.action==='stop').length,1,'Never blindly retry Stop');
        assert.equal(commands.filter(row=>row.action==='request_plan').length,1,'Never replay planner launch');
        assert.deepEqual(errors,[]);
        const evidence=await (await fetch(info.url+'/__fixture__/evidence')).json();
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({commands,stopped,evidence},null,2));
        await page.screenshot({path:path.join(output,'stopped-after-delayed-planner.png')});
        console.log('Follow-up Stop browser evidence:',output);
    } catch(error) {
        if(page) {
            await page.screenshot({path:path.join(output,'failure.png')}).catch(()=>{});
            fs.writeFileSync(path.join(output,'failure-state.json'),JSON.stringify(await page.evaluate(()=>({state:app._swarmState,pending:app._swarmPending,
                refresh:app._swarmStopRefresh,delayed:window.delayedSwarm})).catch(()=>null),null,2));
        }
        console.error('Follow-up Stop evidence:',output);
        throw error;
    } finally {
        if(browser) await browser.close();
        if(info) await fetch(info.url+'/__fixture__/shutdown',{method:'POST'}).catch(()=>{});
        let timer;
        const stopped=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),5000);})]);
        clearTimeout(timer);
        if(!stopped)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
