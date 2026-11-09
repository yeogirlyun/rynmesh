# Ryn node and Rynmesh: product direction

Owner direction, 2026-11-09. This is the long-term product and assignment frame;
acceptance targets below are planned work, not claims of shipped capabilities.
The dated implementation evidence in [Product milestones](PRODUCT_MILESTONES.md)
and the [network economy roadmap](NETWORK_ECONOMY_ROADMAP.md) keeps its original scope.

## Five connected objectives

| Objective | Product requirement | Evidence of progress |
|---|---|---|
| Useful personal node | Convenient apps, content, files, AI, personal projects, entertainment and friend interaction give people a reason to install Ryn today. Personal use works independently of company membership. | A new user completes useful work, saves the result, returns to it and can invite a friend. |
| Common personal/company foundation | Both use the same node, assistant, model, app/plugin, service, task, artifact and interaction contracts. Organizations add employee/member designation, teams and selected service audiences. | A personal node consumes a company service through a normal connector; a second synthetic organization needs no company-specific core changes. |
| Company adoption and extension | A reference company migrates its real online workflows into its company node and exposes the underlying services through apps and AI tools. | Ordinary employees finish complete workflows from their own nodes; the company can add another service without forking the public host. |
| Accumulating personal usefulness | App activity, chats, files and permitted service interactions produce durable history, preferences, artifacts and retrievable context. | The assistant can reopen earlier work, use relevant authorized evidence and continue accurately after restart or conversation continuation. |
| Open applications between participants | Reusable infrastructure supports publishing, discovery, communication, media, professional networks, work and exchange between people. | New activities reuse common contracts; participants control publication, audience, relationships and agreed terms. |

Personal friends and organization membership are distinct relationships over
shared infrastructure. Joining a company does not transfer ownership of a
personal node or unrelated personal history. Company nodes can serve employees,
partners, customers, investors or the public according to the services they expose.
The application and its declared capabilities determine which features a node offers.

## The product loop

```text
useful personal app -> first saved result -> return and continue
  -> relevant accumulated context -> more useful assistance
  -> connect a friend or company -> useful work with others
  -> share a result or invite someone -> more participants and services
```

Loyalty should grow from useful work, continuity, convenience and trust. Users
retain control of their content, memory and connections, with inspect/correct,
export, disconnect and scoped deletion paths. Sharing a result gives recipients
something useful before they have to understand the network.

The assistant improves through relevant retrieval, explicit preferences and
task history. Store sources, scope, timestamps, visible answers, tool results,
files and receipts; keep original records separate from bounded model context.
Relevant company work can contribute within its audience and retention scope.
Model requests use the context appropriate to that task and destination. Never
retain credentials or hidden model reasoning as conversation history. Claims of
model training or autonomous learning require their own implemented evidence.

## Common infrastructure and repository boundary

- Public Rynmesh owns generic nodes, connectivity, discovery, friend/member
  relationship mechanisms, models, assistant tasks, user-controlled memory,
  artifacts, app/plugin lifecycle, service adapters and shared conformance tests.
- A company repository owns its business services, dashboards, operation rules,
  organization mappings, integrations, configuration and migration design.
- An API exposes a service; MCP adapts capabilities for assistants; a plugin
  declares the connection, tools, skills and views; an app provides the user
  experience; the extension framework hosts those capabilities. Apps and AI
  reuse the same business service and result/artifact contracts.
- Corporate additions are reusable organization capabilities plus private
  policy/configuration. Use normal connectors and declared capabilities so
  personal and company deployments can participate in the same network.
- Open-source participation is free of platform commission. Future paid services
  can pay their actual providers through Ryncoin. Its launch follows user utility
  and adoption; the existing alpha is implementation evidence with its own limits.

## Economic sequence: adoption, internal circulation, then exchange

Owner clarification, 2026-11-09:

1. **Now: earn adoption through utility.** Deliver personal apps, company work,
   durable assistant context and useful sharing. Downloads, repeat use and real
   workflows establish demand. Ryncoin launch, advertising and subscriptions
   are later milestones; they are not prerequisites for the next user pilot.
2. **Later: earned internal service credits, called Ryncoin.** Once a meaningful
   user base and commercial demand exist, providers earn from useful content,
   services and verified network work. Participants spend on other providers,
   content and services. Advertising budgets, direct subscriptions and their
   provider payouts use this common network unit. A participant can both earn
   and consume. This initial credit phase has no cash purchase, cash-out or
   fixed fiat value. A user works for others or the network, then uses earned
   credits to enjoy others' services. Issuance rewards follow public useful-work rules; payments
   transfer existing balances, and activity alone does not guarantee minting.
3. **Much later: currency exchange.** At sufficient scale, a reviewed exchange
   service lets participants buy Ryncoin and lets earners sell for supported
   currencies at market prices. Exchange, liquidity, custody and operating rules
   are a separate milestone. The initial credits have no promised redemption;
   future exchange is a later design/operating decision, with no fixed peg.

Select an internal-economy pilot only after evidence of repeat users, useful
providers and commercial demand; numeric launch thresholds remain to be proposed.
Internal circulation reduces the initial product scope. Regulatory treatment
depends on the design and jurisdictions and requires review before launch;
internal-only circulation is not an automatic exemption. See the
[economy roadmap](NETWORK_ECONOMY_ROADMAP.md#launch-sequence-owner-clarification-2026-11-09).

## Near-term delivery: prove both personal and employee value

The next batch has two user journeys backed by shared infrastructure:

1. **Personal:** install/open Ryn, choose a useful existing app, use one's own
   material to get a grounded answer or document, open/save the output, restart
   and continue. Then share a selected result with a friend. Initial examples
   can use reading, Word/PDF or a personal project; the app catalogue is broader.
2. **Employee:** connect once to a company node, discover permitted capabilities,
   query a real business workflow, produce/open a report, review the recipient,
   share it, restart and continue with the saved evidence. Validate two ordinary
   employee identities from separate client homes.

Shared prerequisites: usable installs and app downloads; discoverable actual
capabilities; recoverable tasks; settings and usage retained across continuation;
durable conversations, files and operation receipts; bounded context retrieval;
convenient remembered connections; clear failure recovery and measured latency.
Use the current identity mechanisms and practical audience checks.

For each delivery batch, report the personal-user benefit, employee benefit,
reusable infrastructure change, real acceptance, and remaining dependency. A
small PR may serve one journey; its milestone must keep both journeys progressing.
General improvements return to public Rynmesh; company business code stays private.

## Expansion and success measures

After the first journeys work repeatedly, extend company workflow coverage and
add useful personal/social apps over the same contracts. Media publishing and
viewing, short posts, professional profiles, hiring, commerce and services are
examples. YouTube-, X- and LinkedIn-like experiences illustrate possibilities;
they do not prescribe separate immediate clone projects. Producers and consumers
control their relationships and direct agreements through open rules.

Measure time to first useful result, completed/reopened work, warm-query latency,
artifact usability, successful sharing, manual repeat use and invitations that
lead to a recipient's first useful result. Company adoption also needs an
inventory of workflows migrated, usable by employees, and still dependent on
legacy systems. Use manual pilots or voluntary local diagnostics; this plan
creates no automatic telemetry, monitoring or scheduled work.
