(function () {
  "use strict";

  const OWNER = "yeogirlyun";
  const REPO = "rynmesh";
  const API = `https://api.github.com/repos/${OWNER}/${REPO}`;
  const STATUS_LABELS = [
    "status:available",
    "status:claim-requested",
    "status:reserved",
    "status:in-progress",
    "status:in-review",
    "status:blocked",
  ];

  const cache = {
    get(key) {
      try {
        const record = JSON.parse(sessionStorage.getItem(`rynmesh:${key}`));
        if (record && Date.now() - record.savedAt < 5 * 60 * 1000) return record.value;
      } catch (_) { /* Ignore unavailable or malformed browser storage. */ }
      return null;
    },
    set(key, value) {
      try { sessionStorage.setItem(`rynmesh:${key}`, JSON.stringify({ savedAt: Date.now(), value })); }
      catch (_) { /* The live page still works without cache storage. */ }
    },
  };

  async function github(path, key) {
    const stored = cache.get(key || path);
    if (stored) return stored;
    const response = await fetch(`${API}${path}`, {
      headers: { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" },
    });
    if (!response.ok) {
      const error = new Error(`GitHub returned ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const value = await response.json();
    cache.set(key || path, value);
    return value;
  }

  function el(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(options).forEach(([name, value]) => {
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name === "dataset") Object.assign(node.dataset, value);
      else if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2), value);
      else if (value !== undefined && value !== null) node.setAttribute(name, value);
    });
    const list = Array.isArray(children) ? children : [children];
    list.filter(Boolean).forEach((child) => node.append(child));
    return node;
  }

  function labelsFor(issue) { return issue.labels.map((label) => typeof label === "string" ? label : label.name); }

  function normalizedBody(body) {
    return String(body || "").replace(/\\n/g, "\n");
  }

  function statusFor(issue) {
    const labels = labelsFor(issue);
    const status = STATUS_LABELS.find((name) => labels.includes(name));
    if (status) return status.slice(7);
    if (issue.state === "closed") return "completed";
    return "available";
  }

  function statusText(status) {
    return ({
      available: "Available",
      "claim-requested": "Approval requested",
      reserved: "Reserved",
      "in-progress": "In progress",
      "in-review": "In review",
      blocked: "Blocked",
      completed: "Completed",
    })[status] || status;
  }

  function statusClass(status) {
    return ({
      available: "available",
      "claim-requested": "requested",
      reserved: "reserved",
      "in-progress": "progress",
      "in-review": "review",
      blocked: "blocked",
      completed: "shipped",
    })[status] || "planned";
  }

  function badge(status) {
    return el("span", { class: `badge ${statusClass(status)}`, text: statusText(status) });
  }

  function shortDescription(body) {
    if (!body) return "Open the accepted issue for the complete scope and acceptance criteria.";
    const paragraph = normalizedBody(body)
      .split(/\n\s*\n/)
      .map((part) => part.replace(/^#+\s*/gm, "").trim())
      .find((part) => part && !part.startsWith("- ["));
    if (!paragraph) return "Open the accepted issue for the complete scope and acceptance criteria.";
    return paragraph.length > 190 ? `${paragraph.slice(0, 187).trim()}…` : paragraph;
  }

  function humanDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("en", { year: "numeric", month: "short", day: "numeric" }).format(new Date(value));
  }

  function humanBytes(value) {
    if (!Number.isFinite(value)) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let size = value;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
  }

  function claimFromComments(comments) {
    let claim = null;
    const claimPattern = /<!--\s*rynmesh-claim\s+({[^]*?})\s*-->/;
    const releasePattern = /<!--\s*rynmesh-claim-release\s+({[^]*?})\s*-->/;
    comments.forEach((comment) => {
      const claimMatch = comment.body && comment.body.match(claimPattern);
      const releaseMatch = comment.body && comment.body.match(releasePattern);
      if (claimMatch) {
        try { claim = JSON.parse(claimMatch[1]); } catch (_) { /* Ignore invalid markers. */ }
      }
      if (releaseMatch) claim = null;
    });
    return claim;
  }

  async function claimFor(issue) {
    const status = statusFor(issue);
    if (!["reserved", "claim-requested", "in-progress", "in-review"].includes(status)) return null;
    try {
      const comments = await github(`/issues/${issue.number}/comments?per_page=100`, `comments-${issue.number}`);
      return claimFromComments(comments);
    } catch (_) { return null; }
  }

  function issueLabels(issue) {
    const hidden = new Set(["help wanted", "enhancement", "good first issue", ...STATUS_LABELS]);
    return labelsFor(issue).filter((label) => !hidden.has(label)).slice(0, 4);
  }

  async function workCard(issue) {
    const status = statusFor(issue);
    const claim = await claimFor(issue);
    const card = el("article", { class: "work-card", dataset: { status, labels: labelsFor(issue).join("|").toLowerCase(), search: `${issue.number} ${issue.title} ${issue.body || ""}`.toLowerCase() } });
    card.append(
      el("div", { class: "work-card-top" }, [
        el("span", { class: "issue-number", text: `#${issue.number}` }),
        badge(status),
      ]),
      el("h3", {}, el("a", { href: `/contribute/task/?issue=${issue.number}`, text: issue.title })),
      el("p", { text: shortDescription(issue.body) }),
      el("div", { class: "labels" }, issueLabels(issue).map((label) => el("span", { class: "label", text: label }))),
      el("div", { class: "work-owner", text: claim ? `Primary contributor: @${claim.user}` : status === "available" ? "Ready for a contributor" : "See GitHub for current coordination" }),
      el("div", { class: "work-card-actions" }, [
        el("a", { class: "button button-secondary button-small", href: `/contribute/task/?issue=${issue.number}`, text: "View scope" }),
        status === "available"
          ? el("a", { class: "button button-primary button-small", href: `/contribute/task/?issue=${issue.number}#claim`, text: "Claim" })
          : el("a", { class: "button button-quiet button-small", href: issue.html_url, text: "Follow on GitHub", target: "_blank", rel: "noopener" }),
      ]),
    );
    return card;
  }

  async function getAcceptedIssues() {
    const issues = await github("/issues?state=open&labels=help%20wanted&sort=updated&direction=desc&per_page=100", "accepted-issues");
    return issues.filter((issue) => !issue.pull_request);
  }

  function state(target, title, message, kind = "") {
    target.replaceChildren(el("div", { class: `system-state ${kind}` }, [
      el("h3", { text: title }),
      el("p", { text: message }),
    ]));
  }

  async function renderWork() {
    const target = document.querySelector("[data-work-list]");
    if (!target) return;
    state(target, "Loading accepted work", "Reading live issue status from the public GitHub repository.", "loading");
    try {
      const issues = await getAcceptedIssues();
      if (!issues.length) {
        state(target, "No accepted work is open", "Check GitHub Discussions, test the latest release, or propose a focused improvement.");
        return;
      }
      const cards = await Promise.all(issues.map(workCard));
      target.replaceChildren(...cards);
      target.dataset.ready = "true";
      applyFilters();
      const count = document.querySelector("[data-work-count]");
      if (count) count.textContent = `${cards.length} accepted ${cards.length === 1 ? "item" : "items"}`;
    } catch (error) {
      const rate = error.status === 403 || error.status === 429;
      state(target, rate ? "GitHub rate limit reached" : "GitHub status is temporarily unavailable", rate ? "The public API limit will reset automatically. Open the repository to browse work now." : "The contribution data was not replaced with invented content. Browse the source-of-truth issues on GitHub instead.");
    }
  }

  function applyFilters() {
    const list = document.querySelector("[data-work-list]");
    if (!list || list.dataset.ready !== "true") return;
    const search = (document.querySelector("[data-work-search]")?.value || "").trim().toLowerCase();
    const status = document.querySelector("[data-status-filter]")?.value || "all";
    const checked = Array.from(document.querySelectorAll("[data-label-filter]:checked")).map((input) => input.value.toLowerCase());
    let shown = 0;
    list.querySelectorAll(".work-card").forEach((card) => {
      const matches = (!search || card.dataset.search.includes(search))
        && (status === "all" || card.dataset.status === status)
        && checked.every((label) => card.dataset.labels.split("|").includes(label));
      card.hidden = !matches;
      if (matches) shown += 1;
    });
    let empty = list.querySelector("[data-filter-empty]");
    if (!shown) {
      if (!empty) {
        empty = el("div", { class: "system-state", dataset: { filterEmpty: "true" } }, [
          el("h3", { text: "No work matches these filters" }),
          el("p", { text: "Clear one or more filters to see the accepted backlog." }),
        ]);
        list.append(empty);
      }
    } else if (empty) empty.remove();
  }

  async function renderPreview() {
    const target = document.querySelector("[data-work-preview]");
    if (!target) return;
    state(target, "Loading open work", "Checking the accepted GitHub backlog.", "loading");
    try {
      const issues = (await getAcceptedIssues()).slice(0, 3);
      target.replaceChildren(...await Promise.all(issues.map(workCard)));
    } catch (_) {
      state(target, "Open work lives on GitHub", "View the accepted backlog directly when live status cannot be loaded.");
    }
  }

  function acceptanceCriteria(body) {
    return normalizedBody(body).split("\n")
      .map((line) => line.match(/^\s*-\s*\[[ xX]\]\s*(.+)$/))
      .filter(Boolean)
      .map((match) => match[1].trim());
  }

  function sectionText(body, heading) {
    if (!body) return "";
    body = normalizedBody(body);
    const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = body.match(new RegExp(`##\\s+${escaped}\\s*\\n([\\s\\S]*?)(?=\\n##\\s|$)`, "i"));
    return match ? match[1].replace(/^\s*-\s*\[[ xX]\]\s*/gm, "").trim() : "";
  }

  async function renderTask() {
    const target = document.querySelector("[data-task]");
    if (!target) return;
    const number = new URLSearchParams(location.search).get("issue");
    if (!/^\d+$/.test(number || "")) {
      state(target, "Choose a contribution item", "Return to the contribution center and select accepted work.");
      return;
    }
    state(target, "Loading contribution scope", `Reading issue #${number} from GitHub.`, "loading");
    try {
      const issue = await github(`/issues/${number}`, `issue-${number}`);
      if (issue.pull_request) throw new Error("This number belongs to a pull request.");
      const status = statusFor(issue);
      const claim = await claimFor(issue);
      const criteria = acceptanceCriteria(issue.body);
      document.title = `#${issue.number} ${issue.title} — RynMesh contributions`;

      const main = el("article", { class: "task-main" }, [
        el("div", { class: "work-card-top" }, [el("span", { class: "issue-number", text: `Issue #${issue.number}` }), badge(status)]),
        el("h1", { text: issue.title }),
        el("p", { class: "lede", text: shortDescription(sectionText(issue.body, "Problem") || issue.body) }),
        el("h2", { text: "Accepted outcome" }),
        criteria.length
          ? el("ul", { class: "checklist" }, criteria.map((item) => el("li", { text: item })))
          : el("p", { text: "Read the GitHub issue for the maintainer-approved acceptance criteria." }),
        el("h2", { text: "Complete issue" }),
        el("pre", { text: normalizedBody(issue.body) || "No issue body was provided." }),
      ]);

      const approvalRequired = labelsFor(issue).some((label) => ["needs design", "privacy", "size:large"].includes(label));
      const claimSection = el("section", { id: "claim" }, [
        el("h2", { text: status === "available" ? "Reserve this work" : statusText(status) }),
        status === "available"
          ? el("div", { class: `claim-instructions${approvalRequired ? " warning" : ""}` }, [
              el("p", { text: approvalRequired ? "This item can be reserved, but implementation requires maintainer approval before code changes begin." : "Comment the exact command below on the GitHub issue. The contribution bot will reserve the item atomically for seven days." }),
              el("div", { class: "claim-command" }, [el("span", { text: "/claim" }), el("button", { class: "copy-button", type: "button", text: "Copy", dataset: { copy: "/claim" } })]),
              el("a", { class: "button button-primary", href: issue.html_url, target: "_blank", rel: "noopener", text: "Open issue and claim" }),
            ])
          : el("div", { class: "callout warning" }, [
              el("p", { text: claim ? `This item is currently coordinated by @${claim.user}. You can follow progress or offer focused help on GitHub.` : "This item is not currently available for a new primary reservation. Check GitHub for the latest coordination record." }),
              el("a", { class: "button button-secondary", href: issue.html_url, target: "_blank", rel: "noopener", text: "View coordination" }),
            ]),
      ]);

      const meta = el("section", {}, [
        el("h2", { text: "Work details" }),
        el("dl", { class: "meta-list" }, [
          el("div", { class: "meta-row" }, [el("dt", { text: "Status" }), el("dd", { text: statusText(status) })]),
          el("div", { class: "meta-row" }, [el("dt", { text: "Primary" }), el("dd", { text: claim ? `@${claim.user}` : "Unclaimed" })]),
          el("div", { class: "meta-row" }, [el("dt", { text: "Updated" }), el("dd", { text: humanDate(issue.updated_at) })]),
          el("div", { class: "meta-row" }, [el("dt", { text: "Approval" }), el("dd", { text: approvalRequired ? "Required" : "Standard review" })]),
        ]),
        el("div", { class: "labels" }, issueLabels(issue).map((label) => el("span", { class: "label", text: label }))),
      ]);

      const side = el("aside", { class: "task-side", "aria-label": "Contribution coordination" }, [meta, claimSection]);
      target.replaceChildren(main, side);
      initCopyButtons();
    } catch (_) {
      state(target, "Contribution item unavailable", "This issue could not be read. Return to the live contribution center or open GitHub directly.");
    }
  }

  async function renderRelease() {
    const target = document.querySelector("[data-release]");
    if (!target) return;
    state(target, "Loading the latest verified release", "Reading release assets from GitHub.", "loading");
    try {
      const release = await github("/releases/latest", "latest-release");
      const assets = release.assets || [];
      const packages = [
        { pattern: /macos-aarch64\.dmg$/, title: "macOS · Apple Silicon", detail: "Self-contained desktop app for M-series Macs." },
        { pattern: /macos-x86_64\.dmg$/, title: "macOS · Intel", detail: "Self-contained desktop app for Intel Macs." },
        { pattern: /py3-none-any\.whl$/, title: "Python package", detail: "Node daemon and bundled web interface for Python 3.10+." },
        { pattern: /\.tar\.gz$/, title: "Source archive", detail: "Versioned source generated by the release workflow." },
      ];
      const cards = packages.map((item) => {
        const asset = assets.find((candidate) => item.pattern.test(candidate.name));
        const available = Boolean(asset);
        return el("article", { class: "download-card" }, [
          badge(available ? "available" : "planned"),
          el("h3", { text: item.title }),
          el("p", { text: item.detail }),
          el("div", { class: "download-meta", text: available ? `${release.tag_name} · ${humanBytes(asset.size)} · ${humanDate(release.published_at)}` : "No verified release artifact yet" }),
          available
            ? el("a", { class: "button button-primary", href: asset.browser_download_url, text: "Download verified artifact" })
            : el("span", { class: "button button-secondary", "aria-disabled": "true", text: "Planned" }),
        ]);
      });
      target.replaceChildren(...cards);
      document.querySelectorAll("[data-release-version]").forEach((node) => { node.textContent = release.tag_name; });
      document.querySelectorAll("[data-release-date]").forEach((node) => { node.textContent = humanDate(release.published_at); });
    } catch (error) {
      state(target, "Release data is temporarily unavailable", "Download only from the repository’s GitHub Releases page; the website will not substitute an unverified package.");
    }
  }

  function initCopyButtons() {
    document.querySelectorAll("[data-copy]").forEach((button) => {
      if (button.dataset.copyReady) return;
      button.dataset.copyReady = "true";
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(button.dataset.copy);
          const old = button.textContent;
          button.textContent = "Copied";
          setTimeout(() => { button.textContent = old; }, 1300);
        } catch (_) { button.textContent = "Select and copy"; }
      });
    });
  }

  function initNav() {
    const button = document.querySelector("[data-menu-button]");
    const nav = document.querySelector("[data-main-nav]");
    if (!button || !nav) return;
    button.addEventListener("click", () => {
      const open = nav.dataset.open !== "true";
      nav.dataset.open = String(open);
      button.setAttribute("aria-expanded", String(open));
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initNav();
    initCopyButtons();
    renderWork();
    renderPreview();
    renderTask();
    renderRelease();
    document.querySelectorAll("[data-work-search], [data-status-filter], [data-label-filter]").forEach((control) => control.addEventListener("input", applyFilters));
  });
})();
