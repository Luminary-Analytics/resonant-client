/* Two GUI processes, independent member certificates, actual TLS/PG and owned child. */
const test=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require(process.argv[2]||'playwright');
const {managedGovernanceSkip}=require('./managed_governance.cjs');
const candidate=process.env.SWARM_PACKAGED_EXECUTABLE?path.resolve(process.env.SWARM_PACKAGED_EXECUTABLE):null;
const retention=process.env.SWARM_SHARING_RETENTION==='1';

test('Two managed owners explicitly disclose selected content and accept independent work', {timeout:150000, skip:managedGovernanceSkip()}, async()=>{
    assert.ok(process.env.SONN_GOVERNANCE_TEST_CONFIG);assert.ok(process.env.SWARM_MANAGED_PYTHON);
    const output=fs.mkdtempSync(path.join(os.tmpdir(),'sonn-managed-sharing-browser-'));
    const server=spawn(process.env.SWARM_MANAGED_PYTHON,[path.join(__dirname,'fixtures/swarming_managed_collaboration_ui_server.py'),
        output,process.env.SONN_GOVERNANCE_TEST_CONFIG,...(candidate?['--candidate',candidate]:[])],{cwd:output,windowsHide:true,stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',info,browser;
    const pages=[],commands=[[],[]],responses=[[],[]],errors=[];
    server.stdout.on('data',data=>{stdout+=data;});server.stderr.on('data',data=>{stderr+=data;});
    const exited=new Promise(resolve=>server.once('exit',(code,signal)=>resolve({code,signal})));
    const wait=page=>page.waitForFunction(()=>app._swarmState&&!app._swarmPending);
    async function click(page,name,action,keyboard=false){
        const index=pages.indexOf(page);
        for(let attempt=0;attempt<2;attempt++){
            await wait(page);const offset=commands[index].length;
            const button=page.getByRole('button',{name,exact:true});
            if(keyboard){await button.focus();await page.keyboard.press('Enter');}else await button.click();
            let sent,response;
            for(let n=0;n<120;n++){
                sent=commands[index].slice(offset).find(row=>row.action===action);
                response=sent&&responses[index].find(row=>row.request_id===sent.request_id);
                if(response)break;
                await page.waitForTimeout(50);
            }
            assert.ok(response,'A lost command reply is not permission to replay');
            if(response.error){
                assert.match(response.error,/^(Run revision changed|Refresh the team before changing shared work)/);
                assert.equal(attempt,0);
                await page.getByRole('button',{name:'Refresh team',exact:true}).click();await wait(page);continue;
            }
            if(action.startsWith('managed_sharing_')&&action!=='managed_sharing_prepare'){
                await page.waitForFunction(id=>app._swarmState?.managed_collaboration?.operation?.request_id===id
                    &&!['queued','running'].includes(app._swarmState.managed_collaboration.operation.state),sent.request_id);
                const operation=await page.evaluate(()=>app._swarmState.managed_collaboration.operation);
                assert.equal(operation.state,'completed',JSON.stringify(operation));
            }
            return;
        }
    }
    async function inspect(page,grant){
        await click(page,'Refresh managed agreements','managed_sharing_inspect');
        await page.getByLabel('Managed sharing agreement',{exact:true}).selectOption(grant);
        await click(page,'Inspect managed agreement','managed_sharing_inspect');
    }
    async function getEvidence(){
        const clients=await Promise.all(info.clients.map(async(item,index)=>{
            const value=await(await fetch((item.api_url||item.url)+'/__fixture__/evidence')).json();
            if(item.frozen){
                const view=await pages[index].evaluate(()=>app._swarmState);
                value.views=view?.run?[view]:[];
            }
            return value;
        }));
        return {central:await (await fetch(info.control+'/__fixture__/evidence')).json(),clients};
    }
    try{
        for(let n=0;n<500;n++){
            const line=stdout.split(/\r?\n/).find(value=>value.startsWith('{"clients":'));
            if(line){info=JSON.parse(line);break;}
            if(server.exitCode!==null)throw Error('Fixture failed: '+stderr);
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.ok(info,'Two-GUI fixture did not start: '+stderr);
        browser=await chromium.launch(process.env.SWARM_BROWSER_EXECUTABLE?
            {headless:true,executablePath:process.env.SWARM_BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
        for(let index=0;index<2;index++){
            const page=await browser.newPage({viewport:{width:info.clients[index].frozen?1180:390,height:844}});pages.push(page);page.setDefaultTimeout(25000);
            page.on('pageerror',error=>errors.push(error.message));
            page.on('websocket',socket=>{
                socket.on('framesent',frame=>{const row=JSON.parse(frame.payload);if(row.command==='swarm')commands[index].push(row);});
                socket.on('framereceived',frame=>{const row=JSON.parse(frame.payload);if(row.event==='swarm_state')responses[index].push(row);});
            });
            await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
            await page.goto(info.clients[index].url);
            if(info.clients[index].frozen){
                await page.waitForFunction(()=>window.app?.backends?.ollama?.models?.length);
                await page.locator(`.agent-row[data-session-id="${info.clients[index].session_id}"] .session-title-text`).click();
            }
            await page.waitForFunction(id=>window.app?.currentSessionId===id,info.clients[index].session_id);
            await page.setViewportSize({width:390,height:844});
            await page.locator('#user-input').fill('Preserved ordinary conversation draft '+index);
            await page.getByRole('button',{name:'Team',exact:true}).click();await wait(page);
            await page.getByLabel('Enable team preview').check();await wait(page);
            await page.getByLabel('Execution ownership').selectOption('managed');
            await page.waitForFunction(()=>app._swarmState?.execution_mode==='managed'&&!app._swarmPending);
            await page.getByText('Share with another managed conversation',{exact:true}).click();
            await page.getByLabel('Managed collaboration objective').fill('Independent owner '+index+' collaboration');
            await page.getByLabel('Managed collaboration request allowance').fill('6');
            await click(page,'Prepare managed collaboration team','managed_sharing_prepare');
            // The run has a new captured panel; open its own persistent draft.
            await page.getByText('Share with another managed conversation',{exact:true}).click();
            await page.waitForFunction(()=>app._swarmState?.managed_collaboration?.address&&app._swarmState?.managed?.connection==='connected');
            assert.equal(await page.getByRole('dialog',{name:'Work together'}).evaluate(node=>node.scrollWidth<=node.clientWidth+1),true);
        }
        const [a,b]=pages;
        let evidence=await getEvidence();assert.ok(evidence.central.owners_distinct);
        assert.equal(new Set(evidence.central.gui_pids).size,2);assert.deepEqual(evidence.clients.map(item=>item.owned_children),[0,0]);
        const address=await b.getByLabel('This managed team’s address').inputValue();
        await a.getByText('Offer a managed sharing agreement',{exact:true}).click();
        await a.getByLabel('Other managed team’s address').fill(address);
        await a.getByLabel('Managed sharing purpose').fill('One explicit independent fact investigation');
        await a.getByRole('checkbox',{name:'Work proposals',exact:true}).check();
        await a.getByRole('checkbox',{name:'Artifact references',exact:true}).check();
        await a.getByRole('checkbox',{name:'Exact artifact reference',exact:true}).check();
        await a.getByLabel('Managed accepted request limit').fill('3');
        await click(a,'Offer managed sharing agreement','managed_sharing_offer');
        const grant=await a.evaluate(()=>app._swarmState.managed_collaboration.grants.items[0].grant_id);
        await inspect(b,grant);
        await b.getByRole('button',{name:'Approve inspected managed agreement',exact:true}).focus();
        await b.screenshot({path:path.join(output,'receiver-agreement.png')});
        await click(b,'Approve inspected managed agreement','managed_sharing_approve',true);
        await inspect(a,grant);
        await a.getByText('Compose selected managed content',{exact:true}).click();
        await a.getByLabel('Managed message type',{exact:true}).selectOption('artifact_offer');
        await a.getByLabel('Managed content class',{exact:true}).selectOption('artifact_reference');
        const count=commands[0].length;
        await a.getByLabel('Selected managed message content').fill('{"artifact_id":"invented"}');
        await a.getByRole('button',{name:'Send selected managed content',exact:true}).click();
        assert.equal(commands[0].slice(count).filter(row=>row.action==='managed_sharing_send').length,0);
        await a.getByLabel('Selected managed message content').fill(JSON.stringify(info.artifact));
        await click(a,'Send selected managed content','managed_sharing_send');
        await inspect(a,grant);
        await a.getByLabel('Managed message type',{exact:true}).selectOption('work_request');
        await a.getByLabel('Managed content class',{exact:true}).selectOption('summary');
        const body='Selected peer proposal: inspect only your own fact. PRIVATE transcript is deliberately omitted.';
        const field=a.getByLabel('Selected managed message content');
        await field.fill(body);await field.focus();
        await a.waitForTimeout(1300);assert.equal(await field.inputValue(),body);assert.equal(await field.evaluate(node=>node===document.activeElement),true);
        await click(a,'Send selected managed content','managed_sharing_send');
        await inspect(b,grant);
        assert.equal(await b.getByText(body,{exact:true}).count(),0);
        const rows=b.locator('.swarm-collaboration-message');
        const artifact=rows.filter({has:b.locator('[data-ms-message-status]').filter({hasText:'artifact offer'})});
        await artifact.getByRole('button',{name:'Read selected managed message',exact:true}).click();
        await b.waitForFunction(()=>app._swarmState?.managed_collaboration?.selected_content?.kind==='artifact_offer');
        assert.deepEqual(JSON.parse(await artifact.locator('[data-ms-content]').innerText()),info.artifact);
        assert.equal(await b.getByText('Explicit owner-authored fixture evidence',{exact:true}).count(),0);
        if(retention){
            const messageId=await b.evaluate(()=>app._swarmState.managed_collaboration.selected_content.message_id);
            const response=await fetch(info.control+'/__fixture__/delete-sharing',{method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({kind:'message',resource_id:messageId})});
            assert.equal(response.status,200);
            await inspect(b,grant);
            assert.match(await artifact.locator('[data-ms-message-status]').innerText(),/deleted/);
            assert.equal(await artifact.getByRole('button',{name:'Read selected managed message',exact:true}).isDisabled(),true);
            assert.equal(await artifact.locator('[data-ms-content]').innerText(),'Selected content is withheld until you explicitly read it.');
            await artifact.scrollIntoViewIfNeeded();
            await b.screenshot({path:path.join(output,'deleted-message-metadata.png')});
        }
        const work=rows.filter({has:b.locator('[data-ms-message-status]').filter({hasText:'work request'})});
        await work.getByRole('button',{name:'Read selected managed message',exact:true}).click();
        await b.waitForFunction(()=>app._swarmState?.managed_collaboration?.selected_content?.kind==='work_request');
        assert.equal(await work.locator('[data-ms-content]').innerText(),body);
        const beforeSwitch=commands[1].length;
        await b.getByLabel('Managed sharing agreement',{exact:true}).selectOption('');
        assert.equal(await b.getByText(body,{exact:true}).count(),0);
        assert.equal(commands[1].slice(beforeSwitch).filter(row=>row.action.startsWith('managed_sharing_')).length,0);
        await inspect(b,grant);
        assert.equal(await b.getByText(body,{exact:true}).count(),0);
        await work.getByRole('button',{name:'Read selected managed message',exact:true}).click();
        await b.waitForFunction(()=>app._swarmState?.managed_collaboration?.selected_content?.kind==='work_request');
        assert.equal((await getEvidence()).clients[1].owned_children,0);
        await work.getByLabel('My managed investigation objective').fill('Read my own fact.txt and report a factual finding');
        await work.getByLabel('My managed readable folders').fill('fact.txt');
        await work.getByLabel('My managed request allowance').fill('3');
        await work.getByLabel('My managed work acceptance notes').fill('I selected my own file and budget independently.');
        await click(b,'Accept and start my managed investigation','managed_sharing_accept_work');
        for(let n=0;n<80;n++){evidence=await getEvidence();if(evidence.clients[1].provider_entered)break;await b.waitForTimeout(100);}
        assert.equal(evidence.clients[1].provider_entered,true);
        if(candidate){
            const children=evidence.clients[1].children.filter(row=>row.argv.includes('--swarm-worker'));
            assert.equal(children.length,1);
            assert.equal(path.resolve(children[0].exe).toLowerCase(),candidate.toLowerCase());
            assert.ok(evidence.clients[1].views[0].run.process_observations.some(row=>row.pid===children[0].pid));
        }
        assert.equal(await work.getByRole('button',{name:'Accept and start my managed investigation',exact:true}).isVisible(),false);
        await inspect(a,grant);
        await click(a,'Revoke managed agreement','managed_sharing_revoke');
        await click(a,'Stop team','stop');
        await a.waitForFunction(()=>app._swarmState.run.run.state==='cancelled');
        evidence=await getEvidence();assert.equal(evidence.clients[1].views[0].run.attempts[0].cancel_requested,0);
        assert.equal(evidence.clients[1].views[0].run.attempts[0].process_state,'running');
        await fetch((info.clients[1].api_url||info.clients[1].url)+'/__fixture__/release',{method:'POST'});
        await b.waitForFunction(()=>app._swarmState?.run?.submissions?.length===1&&app._swarmState.run.attempts[0].process_state==='stopped'&&!app._swarmPending);
        assert.equal(await b.evaluate(()=>app._swarmState.run.work_items[0].state),'submitted');
        await b.locator('.swarm-worker > summary').click();
        await b.getByLabel('Review notes for Worker 1',{exact:true}).fill('Reviewed the receiver-owned fact and retained read observation.');
        await click(b,'Accept findings','review_read_result');
        await click(b,'Complete team','complete');
        await b.waitForFunction(()=>app._swarmState.run.run.state==='completed');
        await b.screenshot({path:path.join(output,'receiver-completed.png')});
        await inspect(a,grant);
        assert.equal(await a.locator('[data-ms-content]').count(),0);
        await a.screenshot({path:path.join(output,'sender-stopped-revoked.png')});
        if(retention){
            const response=await fetch(info.control+'/__fixture__/delete-sharing',{method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({kind:'terms',resource_id:grant})});
            assert.equal(response.status,200);
            await inspect(a,grant);
            assert.equal(await a.locator('[data-ms-status]').innerText(),'Agreement deleted. Selected content is unavailable.');
            assert.equal(await a.getByRole('button',{name:'Revoke managed agreement',exact:true}).isDisabled(),true);
            assert.equal(await a.locator('[data-ms-content]').count(),0);
            await a.screenshot({path:path.join(output,'deleted-agreement-metadata.png')});
        }
        evidence=await getEvidence();assert.equal(evidence.central.counts.sharing_acceptances,1);
        assert.equal(evidence.central.counts.sharing_messages,2);assert.equal(evidence.central.counts.host_requests,2);
        assert.deepEqual(evidence.clients.map(item=>item.owned_children),[0,1]);
        assert.ok(evidence.clients.every(item=>!item.private_configuration_in_model&&!item.peer_proposal_in_model&&!item.live_providers_called));
        assert.deepEqual(errors,[]);
        if(candidate){
            for(let index=0;index<2;index++){
                const log=fs.readFileSync(path.join(output,`owner-${index}`,'candidate-stream.log'),'utf8');
                assert.match(log,/Application startup complete/);assert.match(log,/WebSocket.*accepted/);
                for(const forbidden of ['Traceback (most recent call last)','BEGIN PRIVATE KEY','managed-0.json','managed-1.json','first.key','second.key'])assert.equal(log.includes(forbidden),false);
                assert.equal(evidence.clients[index].children.filter(row=>row.argv.includes('--swarm-worker')).length,0);
                const asset=await fetch(info.clients[index].url+'/static/managed_collaboration_view.js');
                assert.equal(asset.status,200);
                assert.equal(Buffer.compare(Buffer.from(await asset.arrayBuffer()),fs.readFileSync(path.join(path.dirname(candidate),'_internal','lumi','gui','static','managed_collaboration_view.js'))),0);
            }
            assert.equal(evidence.clients[0].requests.length,0);assert.equal(evidence.clients[1].requests.length,2);
            assert.equal(evidence.clients[1].requests.filter(row=>row.observed_file_result).length,1);
        }
        fs.writeFileSync(path.join(output,'evidence.json'),JSON.stringify({...evidence,candidate:info.clients,
            kind:candidate?'frozen-two-owner-managed-sharing':'source-two-owner-managed-sharing',retention,commands,responses:responses.flat().filter(row=>row.error)},null,2));
        console.log('Managed collaboration browser evidence:',output);
    }catch(error){
        for(let index=0;index<pages.length;index++){
            const page=pages[index];
            await page.screenshot({path:path.join(output,`failure-${index}.png`)}).catch(()=>{});
            fs.writeFileSync(path.join(output,`failure-${index}.txt`),await page.locator('body').innerText().catch(()=>''));
            fs.writeFileSync(path.join(output,`state-${index}.json`),JSON.stringify(await page.evaluate(()=>app._swarmState||{}).catch(()=>({})),null,2));
        }
        fs.writeFileSync(path.join(output,'commands.json'),JSON.stringify({commands,responses:responses.flat().filter(row=>row.error),errors},null,2));
        console.error('Managed sharing browser failure evidence:',output);throw error;
    }finally{
        if(browser)await browser.close();server.stdin.end();
        let timer;const result=await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),12000);})]);
        clearTimeout(timer);if(!result)server.kill();
        fs.writeFileSync(path.join(output,'server.log'),stdout+'\n'+stderr);
    }
});
