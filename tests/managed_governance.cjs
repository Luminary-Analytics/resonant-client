'use strict';
// Managed-team browser tests need the governance service, which lives in Lumi
// Cloud rather than this repository, plus a disposable PostgreSQL database and an
// isolated fixture Python. Without all three they skip with this named reason.
// A configured but wrong LUMI_GOVERNANCE_SOURCE is not skipped: the Python
// fixture stops with an error and the test fails.
function managedGovernanceSkip(){
    const missing=[
        ['LUMI_GOVERNANCE_SOURCE','a Lumi Cloud checkout with services/governance'],
        ['SONN_GOVERNANCE_TEST_CONFIG','a disposable PostgreSQL configuration'],
        ['SWARM_MANAGED_PYTHON','an isolated GUI/TLS fixture Python'],
    ].filter(([name])=>!process.env[name]).map(([name,what])=>`${name} (${what})`);
    return missing.length?`managed governance not configured; set ${missing.join(', ')}`:false;
}
module.exports={managedGovernanceSkip};
