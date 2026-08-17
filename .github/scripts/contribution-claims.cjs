"use strict";

const CLAIM_DAYS = 7;
const STATUS_LABELS = [
  "status:available",
  "status:claim-requested",
  "status:reserved",
  "status:in-progress",
  "status:in-review",
  "status:blocked",
];
const APPROVAL_LABELS = new Set(["needs design", "privacy", "size:large"]);
const MAINTAINER_PERMISSIONS = new Set(["admin", "maintain", "write"]);

function parseCommand(body) {
  const firstLine = String(body || "").trim().split(/\r?\n/, 1)[0].trim();
  const match = firstLine.match(/^\/(claim|release|approve|extend)\s*$/i);
  return match ? match[1].toLowerCase() : null;
}

function marker(kind, data) {
  return `<!-- rynmesh-${kind} ${JSON.stringify(data)} -->`;
}

function markerData(body, kind) {
  const pattern = new RegExp(`<!--\\s*rynmesh-${kind}\\s+({[\\s\\S]*?})\\s*-->`);
  const match = String(body || "").match(pattern);
  if (!match) return null;
  try { return JSON.parse(match[1]); } catch (_) { return null; }
}

function activeClaim(comments) {
  let active = null;
  [...comments]
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
    .forEach((comment) => {
      const claim = markerData(comment.body, "claim");
      const release = markerData(comment.body, "claim-release");
      if (claim) active = claim;
      if (release) active = null;
    });
  return active;
}

function expiresAt(now, days = CLAIM_DAYS) {
  return new Date(now.getTime() + days * 24 * 60 * 60 * 1000).toISOString();
}

function labelNames(issue) {
  return (issue.labels || []).map((label) => typeof label === "string" ? label : label.name);
}

function needsApproval(issue) {
  return labelNames(issue).some((label) => APPROVAL_LABELS.has(label));
}

async function permissionFor(github, owner, repo, username) {
  try {
    const response = await github.rest.repos.getCollaboratorPermissionLevel({ owner, repo, username });
    return response.data.user.permission;
  } catch (_) {
    return "none";
  }
}

async function setStatus(github, owner, repo, issueNumber, currentLabels, nextStatus) {
  for (const label of STATUS_LABELS) {
    if (!currentLabels.includes(label) || label === nextStatus) continue;
    try { await github.rest.issues.removeLabel({ owner, repo, issue_number: issueNumber, name: label }); }
    catch (error) { if (error.status !== 404) throw error; }
  }
  if (!currentLabels.includes(nextStatus)) {
    await github.rest.issues.addLabels({ owner, repo, issue_number: issueNumber, labels: [nextStatus] });
  }
}

async function post(github, owner, repo, issueNumber, body) {
  await github.rest.issues.createComment({ owner, repo, issue_number: issueNumber, body });
}

async function listComments(github, owner, repo, issueNumber) {
  return github.paginate(github.rest.issues.listComments, { owner, repo, issue_number: issueNumber, per_page: 100 });
}

async function handleComment({ github, context, core, now = new Date() }) {
  const command = parseCommand(context.payload.comment && context.payload.comment.body);
  if (!command || context.payload.issue.pull_request) return;

  const { owner, repo } = context.repo;
  const issue = context.payload.issue;
  const issueNumber = issue.number;
  const actor = context.actor;
  const labels = labelNames(issue);
  const comments = await listComments(github, owner, repo, issueNumber);
  const claim = activeClaim(comments);
  const permission = await permissionFor(github, owner, repo, actor);
  const maintainer = MAINTAINER_PERMISSIONS.has(permission);

  if (command === "claim") {
    if (issue.state !== "open") return;
    if (!labels.includes("help wanted") && !labels.includes("good first issue")) {
      await post(github, owner, repo, issueNumber, "This issue is not currently marked as accepted contributor work. Please discuss scope with a maintainer before implementation.");
      return;
    }
    if (claim) {
      const message = claim.user === actor
        ? `@${actor}, you already hold the primary reservation for this issue.`
        : `This issue is already reserved by @${claim.user}. You are welcome to offer focused collaboration without creating a second primary implementation.`;
      await post(github, owner, repo, issueNumber, message);
      return;
    }
    const approval = needsApproval(issue);
    const state = approval ? "claim-requested" : "reserved";
    const record = {
      user: actor,
      state,
      claimedAt: now.toISOString(),
      expiresAt: expiresAt(now),
    };
    await setStatus(github, owner, repo, issueNumber, labels, `status:${state}`);
    const message = approval
      ? `Primary reservation requested by @${actor}. This issue is now locked against duplicate claims while a maintainer reviews the design-sensitive scope. Do not begin implementation until approval.\n\nA maintainer can comment \`/approve\`; either the claimant or a maintainer can comment \`/release\`.`
      : `Reserved for @${actor} through ${record.expiresAt.slice(0, 10)}. Please post progress or open a linked draft pull request within seven days.\n\nComment \`/extend\` to renew an active reservation or \`/release\` if plans change.`;
    await post(github, owner, repo, issueNumber, `${marker("claim", record)}\n${message}`);
    return;
  }

  if (!claim) {
    await post(github, owner, repo, issueNumber, `There is no active primary reservation to ${command}.`);
    return;
  }

  if (command === "approve") {
    if (!maintainer) {
      await post(github, owner, repo, issueNumber, `@${actor}, maintainer permission is required to approve design-sensitive work.`);
      return;
    }
    const record = { ...claim, state: "reserved", approvedBy: actor, approvedAt: now.toISOString(), expiresAt: expiresAt(now) };
    await setStatus(github, owner, repo, issueNumber, labels, "status:reserved");
    await post(github, owner, repo, issueNumber, `${marker("claim", record)}\nApproved for implementation by @${actor}. The primary reservation remains with @${claim.user} through ${record.expiresAt.slice(0, 10)}.`);
    return;
  }

  const actorOwnsClaim = claim.user === actor;
  if (!actorOwnsClaim && !maintainer) {
    await post(github, owner, repo, issueNumber, `@${actor}, only the current primary contributor or a maintainer can ${command} this reservation.`);
    return;
  }

  if (command === "extend") {
    const record = { ...claim, extendedBy: actor, extendedAt: now.toISOString(), expiresAt: expiresAt(now) };
    await post(github, owner, repo, issueNumber, `${marker("claim", record)}\nReservation extended for @${claim.user} through ${record.expiresAt.slice(0, 10)}.`);
    return;
  }

  if (command === "release") {
    const record = { user: claim.user, releasedBy: actor, releasedAt: now.toISOString() };
    await setStatus(github, owner, repo, issueNumber, labels, "status:available");
    await post(github, owner, repo, issueNumber, `${marker("claim-release", record)}\nPrimary reservation released by @${actor}. This issue is available again.`);
  }
}

async function expireReservations({ github, context, core, now = new Date() }) {
  const { owner, repo } = context.repo;
  const seen = new Set();
  for (const status of ["status:reserved", "status:claim-requested"]) {
    const issues = await github.paginate(github.rest.issues.listForRepo, { owner, repo, state: "open", labels: status, per_page: 100 });
    for (const issue of issues) {
      if (issue.pull_request || seen.has(issue.number)) continue;
      seen.add(issue.number);
      const comments = await listComments(github, owner, repo, issue.number);
      const claim = activeClaim(comments);
      if (!claim || !claim.expiresAt || new Date(claim.expiresAt) > now) continue;
      const labels = labelNames(issue);
      const record = { user: claim.user, releasedBy: "rynmesh-contribution-bot", releasedAt: now.toISOString(), reason: "inactive-reservation-expired" };
      await setStatus(github, owner, repo, issue.number, labels, "status:available");
      await post(github, owner, repo, issue.number, `${marker("claim-release", record)}\nThe reservation held by @${claim.user} expired without an extension. This issue is available again; prior work and discussion remain welcome.`);
      core.info(`Released expired reservation on #${issue.number}`);
    }
  }
}

module.exports = {
  CLAIM_DAYS,
  STATUS_LABELS,
  activeClaim,
  expireReservations,
  expiresAt,
  handleComment,
  marker,
  markerData,
  needsApproval,
  parseCommand,
};
