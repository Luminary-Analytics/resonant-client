# Lumi language

This glossary defines terms for the proposed swarming feature. It describes the
product vocabulary, not a claim that swarming is implemented or released.

## Language

**Session**:
A saved conversation in one project, owned by its user.
_Avoid_: using session to mean a worker or a single model request.

**Swarm**:
A session's explicitly enabled team of agents working toward a shared objective
under one supervisor.
_Avoid_: using swarm to mean every agent in an organization.

**Swarm run**:
One execution of a swarm's objective with its own plan, assignments, decisions,
and outcome; a session can retain several historical runs.
_Avoid_: using run to mean an individual model request.

**Supervisor**:
The accountable authority for a swarm run's lifecycle, assignments, permissions,
and acceptance of work.
_Avoid_: using supervisor as a synonym for the model alone.

**Coordinator**:
The agent that proposes the plan, delegates work, resolves questions, and
synthesizes the swarm's result within the supervisor's authority.
_Avoid_: director when referring to the new swarming product.

**Worker**:
An agent performing one scoped assignment in a swarm.
_Avoid_: employee when referring to an ordinary temporary worker.

**Work item**:
A bounded part of the objective with an expected result, dependencies, and
acceptance criteria.
_Avoid_: task when its meaning could be confused with a session.

**Attempt**:
One execution of a work item; a revision or reassignment creates another attempt
without replacing the earlier evidence.
_Avoid_: overwriting an attempt to represent a retry.

**Handoff**:
A worker's submitted result, supporting artifacts, limitations, and requested
next action.
_Avoid_: treating a handoff as accepted work.

**Acceptance evidence**:
Attributable observations that support a particular acceptance criterion for a
particular attempt and version of the work.
_Avoid_: treating a worker's success statement as proof.

**Human member**:
A person using Lumi personally or through an organization.
_Avoid_: employee when it could be confused with the separate AI Employee feature.

**Run owner**:
The human member accountable for starting and directing a swarm run.
_Avoid_: treating ownership as unrestricted organization administration.

**Collaboration grant**:
Explicit permission for specified swarms to exchange specified information or
request specified work for a limited purpose.
_Avoid_: treating project membership as permission to share all session content.

**Managed run**:
A swarm run subject to an organization's enforceable execution and governance
requirements.
_Avoid_: calling local activity enterprise-enforced solely because it is reported.
