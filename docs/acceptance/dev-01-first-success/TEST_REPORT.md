# DEV-01 测试报告

日期：2026-09-04

## Focused backend

```text
.venv/Scripts/python -m pytest tests/test_first_success.py tests/test_digest.py tests/test_reader_and_steering.py -q
54 passed, 2 warnings in 2.47s
```

覆盖：空状态、原子/幂等、损坏恢复、ready/awaiting/completed、缓存、全部来源失败、
真实 opened/bookmark、dismiss/reset、export/erase、feature flag、里程碑写失败不影响原行为。

## Full backend

```text
.venv/Scripts/python -m pytest tests -q
601 passed, 1 skipped, 6 warnings in 81.74s
```

唯一 skip 为仓库既有跳过项；warnings 为 Starlette/httpx 与 FastAPI lifespan 弃用提示。

## Backend static check

```text
.venv/Scripts/python -m ruff check rynmesh tests
All checks passed!
```

## Webapp

```text
npm test
Test Files 10 passed (10)
Tests      40 passed (40)

npm run lint
passed

npm run build
1740 modules transformed; built successfully
```

新增 UI 测试验证真实推荐打开后进入 `awaiting_signal`，保存后进入 `completed`，以及
可恢复来源失败不会要求模型或 Peer。

## 浏览器 E2E

使用本地 Vite + fixture，只验证 UI 与 client 状态契约，不冒充真实网络或桌面物理验收。

1. 首次进入自动打开 `Open one real recommendation`；
2. 打开 `Mira Studio micro-essays on stepped balconies`；
3. 页面进入 `Save one useful signal`；
4. 保存后显示 `Your Ryn is already learning`；
5. Home 同步显示可选的本地 AI 下一步。
