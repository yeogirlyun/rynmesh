# Ryn English UI specification
Updated: 2026-09-22
Branch: `development/personal-first-ui`, created from local `codex/local-inference-api-p2p` at `7009670`.
Status: design specification and ImageGen concepts; application implementation has not started.

## Product direction
Personal-first, network-capable: connect your own devices first, then share selected services with other people's devices. English is the default product language. Light, Dark and System appearance are required. The earlier Chinese boards remain archived for comparison.

## Information architecture
- Home: personal overview and quick access.
- Services: use capabilities on your devices or devices shared with you; add services on the current device.
- Devices: full connection and access management, including device names, nicknames and private notes.
- Tasks: requests initiated by you and work processed on the current device.
- Settings: current-device preferences.
- More: Explore and Messages, preserving existing content and peer messaging features.
- AI chat is a Services subpage. Device editing, pairing and sharing are dialogs, not new top-level destinations.

The canonical sidebar order is Home, Services, Devices, Tasks, More (Explore, Messages), then Settings. Workspace selection precedes the navigation. Generated boards are visual proposals: minor icon or shell alignment drift must be resolved to this specification when implemented.

## English terminology

| Concept | Product copy | Notes |
|---|---|---|
| 我的空间 | Home | Avoid abstract "My Space". |
| 节点 | Devices | "Node" may appear only in technical details. |
| 我的设备 | My devices | Ownership confirmed, not inferred from network membership. |
| 其他人共享的 | Shared with me | Does not mean unrestricted access to the entire device. |
| 私人 AI | AI chat | Describes the activity without implying absolute privacy. |
| 节点名称 | Device name | Owner-controlled and visible to connected devices. |
| 本地别名 | Nickname | Visible only to the local user. |
| 私人备注 | Private note | Visible only to the local user. |
| 新增设备 | Add device | Opens pairing, not a machine purchase flow. |
| 开启服务 | Add service | Sets up a service on the current device. |
| 使用 AI | Open chat | Concrete action rather than generic "Use". |
| 接口接入 | API access | Advanced workflow for apps and agents. |
| 分享服务 | Share service / Share services | Singular or plural follows selection. |
| 授权管理 | Manage access | Owner-controlled. |
| 撤销授权 | Revoke access | Distinct from Disconnect. |
| 保存编辑 | Save changes | Device metadata changes. |
| 保存授权 | Save access | Service access changes. |
| 本机 | This device | Keep consistent in labels and connection diagrams. |

## Names and notes
- Owner device name: required after trimming, up to 32 characters.
- Shared device nickname: optional, up to 32 characters. Empty falls back to the owner's name.
- Private note: optional, up to 200 characters; local storage by default, not included in publishing.
- Notes never configure schedules or automation.
- Renaming does not change stable device identity or access rules.
- Editing owner data requires verified management authority; pairing alone does not confer this.
- Home, Services, Devices and Tasks resolve names using the same function.
- UI language is English, but user-entered names and notes may contain any supported Unicode characters.

## Visual and platform contract
- Desktop devices use the same monitor-only symbol; no computer towers.
- Laptops use a laptop symbol. Device type is stable across all pages.
- AI uses the simple sparkle asset; never a metal turbine or vendor logo.
- Services use a single icon family and consistent optical sizes.
- Primary actions use muted slate blue; status uses text plus restrained color.
- Light and Dark share geometry, navigation, labels and focus behavior.
- System theme follows the OS; explicit Light/Dark overrides are persisted.
- Windows/Linux shortcut hint: Ctrl+K; macOS: Command+K. Use platform-native window controls.
- Use sentence case, explicit action verbs, and short helper text.
- Keyboard access, focus visibility, contrast, zoom and narrow-window behavior require implementation verification; mockups do not prove accessibility.
- Generated images remain references, not text or layout source files. Canonical labels in this document take precedence over image rendering drift.

## What the UX evidence supports
A consistent side navigation with a settings entry is a recognized desktop pattern ([Microsoft NavigationView](https://learn.microsoft.com/en-us/windows/apps/develop/ui/controls/navigationview)).
Using familiar terms rather than system-oriented jargon follows [NN/g's match-to-the-real-world principle](https://www.nngroup.com/articles/match-system-real-world/).
These support the structural and wording choices; they do not prove that all international users will prefer this design.

Before release, test with English-speaking target users:
1. Pair a laptop with a home PC without guidance.
2. Find and open AI chat on the intended device.
3. Rename a device and explain who can see its private note.
4. Share one service with a friend, then revoke it.
5. Recover from an offline processing device.

Document conversion and transcription are illustrative future capabilities, not claims of implemented functionality.

## Deliverables
[Browse all 8 English light/dark boards](index.html).
ImageGen prompts are preserved in [prompts.json](prompts.json).
No deployment, branch push or application-code change was performed in this design pass.

