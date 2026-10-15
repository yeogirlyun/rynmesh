# 기기 검색과 AI 리소스 검색의 일관성 재설계

상태: 제안 (코드 변경 없음). 작성일: 2026-10-14. 브랜치: `feature/ryn-integrated-workspace`.

## 1. 증상

같은 노드에서 같은 시점에 두 화면이 서로 모순된 상태를 보여 준다.

| 화면 | 보이는 것 |
|---|---|
| Home → My devices (1) | `ziling-vm`(이 기기)만 표시. `ziling`은 없음 |
| Services → All | `ziling · Discovered device` 소속의 Antigravity / ChatGPT / Z.ai GLM이 **Ready**, **Open chat** 버튼과 함께 표시 |
| Devices → Discovered | `ziling` **Online · Route unconfirmed** |

즉 "기기는 안 보이는데 그 기기의 AI 리소스는 바로 쓸 수 있는 것처럼" 보인다.

## 2. 근본 원인 (코드와 라이브 노드로 검증)

기기와 AI 리소스가 **서로 다른 두 개의 discovery 파이프라인**을 타고, 화면 단에서 `peer_id`로 뒤늦게 합쳐진다.
두 파이프라인은 레코드 종류, TTL, 생존 판단 근거, 신뢰 모델이 모두 다르다.

| | 기기 (devices) | AI 리소스 (services) |
|---|---|---|
| 진입점 | `peer_http.discover_peer_items` → `store.discover_peers` | `llm_package.routes.discover` → `store.list_job_capacities` |
| registry 레코드 | peer record | job capacity record (`metadata.llm_services`) |
| TTL | 24시간 (`RYNMESH_DISCOVERY_MAX_AGE_HOURS`) | 1시간 (`max_age_hours=1`) |
| 생존(online) 근거 | 이 노드의 능동 HTTP probe (`POST /api/local/peers/health`) | **provider가 스스로 게시한 `online` 값** (`public_status`: `health ok and accepting_orders`) |
| 신뢰 | 관계(self/owned/space member)는 로컬 저장소에서만 부여 | 광고 그대로 수용, `binding_verified: False`로 표시만 함 |
| 접근 정책 | 해당 없음 | `explicit_friends`만 제외하고 `personal_space` 광고도 목록에 포함 |

위치:

- `rynmesh/llm_package/routes.py:1549-1614` `discover()`: capacity 광고를 `binding_verified=False`로 그대로 서비스 목록에 추가 (`:1586` TTL 1h, `:1612` 광고 수용).
- `rynmesh/llm_package/routes.py:337-350` `public_status()`: `online`은 provider 자기 보고.
- `rynmesh/llm_package/routes.py:435` 실제 인가는 **호출 시점에 provider 쪽**에서 `space.allows_ai(from_peer_id)`로 비로소 검사.
- `rynmesh/peer_http.py:891-909` `discover_peer_items` (TTL 24h), `:1427-1444` `peers/health` 능동 probe.
- `webapp/src/personal/model.tsx:286-295` 서비스 provider가 peers에 없으면 서비스 레코드만으로 **기기를 합성**한다.
- `webapp/src/personal/model.tsx:312-337` 서비스의 `online`에 `record.online`(자기 보고)을 그대로 사용.
- `webapp/src/personal/Pages.tsx:249-257` Services 필터는 `own`만 보고 신뢰/검증/기기 도달성은 보지 않는다.

라이브 노드(이 VM) 관측값:

```
PEERS     ziling   src=cache  lastSeen=06:37:10Z  probe: online=True http=True lan=False
SERVICES  ziling × 3 (antigravity-cli, codex-cli, src-…)
          online=True  access_policy=personal_space  binding_verified=False
```

해석: ziling이 올린 서비스 3건은 "ziling의 Personal Space 구성원용" 광고다. 이 VM은 구성원이 아니므로
`own=false`라 Home의 My devices에서는 빠지는 것이 맞다. 그런데 서비스 쪽은 같은 신뢰 판단을 거치지 않고
"Ready / Open chat"으로 노출된다. 두 화면이 서로 다른 필터와 서로 다른 진실 공급원을 쓰는 것이 원인이다.

부수 문제:

1. 서비스의 `online`(자기 보고, 최대 1시간 stale)과 기기의 `online`(능동 probe)이 서로 다를 수 있다.
2. 서비스 레코드만 있는 provider가 "기기"로 합성되어 기기 목록의 의미(관계/도달성)가 흐려진다.
3. 사용자는 "Ready"를 보지만 실제 호출은 provider의 인가 단계에서 거부된다. 오류가 나중에야 나타난다.
4. TTL과 갱신 주기가 파이프라인마다 달라 한쪽만 사라지거나 남는 구간이 생긴다.

## 3. 설계 원칙 (불변식)

- **I1 서비스는 기기의 속성이다.** 디렉터리에 없는 peer의 서비스는 노출하지 않는다. 서비스만 알려진 peer는 기기도 "Discovered"로 생긴다.
- **I2 생존은 관측으로만 정한다.** `online`은 이 노드의 probe(또는 서명된 heartbeat)에서만 나온다. provider의 자기 보고는 `online_claim`이라는 참고값이다.
- **I3 서비스 상태 ≤ 기기 상태.** 기기가 offline/unconfirmed이면 서비스는 "Ready"가 될 수 없다.
- **I4 신뢰와 발견을 분리한다.** 발견(discovered) ≠ 사용 가능(usable). 사용 가능 여부는 `정책 ∩ 관계 ∩ 검증`의 함수다. registry는 어떤 권한도 부여하지 않는다(`personal_space.py`의 기존 원칙과 동일).
- **I5 단일 사실 공급원.** Home, Devices, Services는 같은 디렉터리 스냅샷의 서로 다른 **필터**일 뿐이다. 화면별 합성 로직은 두지 않는다.

## 4. 목표 구조

```
Collectors ──► Observation ──► PeerDirectory ──► Views
 LAN multicast                  (peer_id 키,        /api/local/directory   (신규, 전체)
 LAN HTTP probe                  단일 writer,        /api/local/peers       (호환 뷰)
 registry peer records           관측 종류별 TTL)    /api/local/llm/services(호환 뷰)
 registry capacity records            ▲
 peer exchange (/api/v1/peers)        │
 friend / device-sync / space    Prober (단일 스케줄러, route별 백오프)
 manual endpoints
```

데이터 모델:

```ts
PeerEntry {
  peer_id; name: { display; claimed[]; hostname? }
  relationship: "self" | "owned" | "space_member" | "friend" | "discovered"   // 로컬 저장소에서만 부여
  endpoints: [{ url; route: "local"|"lan"|"vpn"|"public"|"relay"|"unknown";
                source; observed_at; last_ok_at; last_error }]
  presence: { state: "online"|"offline"|"unconfirmed"; basis: "probe"|"heartbeat"|"self";
              checked_at; via_endpoint }
  freshness: { first_seen; last_seen; sources[]; stale: boolean }
  services: ServiceEntry[]
}

ServiceEntry {
  service_id; peer_id; title; adapter; model_alias
  advertised: { policy: "public"|"personal_space"|"explicit_friends"; online_claim; published_at }
  verification: { state: "unverified"|"verified"; verified_at? }
  access: { state: "usable"|"needs_pairing"|"needs_space"|"peer_offline"|"denied"; reason }
}
```

파생 규칙:

- `access.state = usable` ⇔ `presence.state = online` ∧ 정책이 관계를 허용 ∧ (`verified` 또는 호출 시 검증 가능).
- UI의 "Ready / Open chat" ⇔ `usable`.
- `relationship ∈ {self, owned, space_member(active)}`가 현재의 `own`에 해당한다.
- TTL은 관측 종류별로 한 곳(디렉터리)에서만 정의한다. LAN 45/100초, registry 1시간, cache 24시간(단 `stale=true`로 표시).

## 5. 화면 반영

- **Home**: My devices는 `relationship ∈ own` 필터를 유지하고, `discovered`가 있으면 "Nearby / Discovered (n)" 줄을 추가해 ziling이 보이게 한다 (상태: Online · Not connected).
- **Devices**: 탭(My devices / Discovered)은 유지하되 같은 디렉터리를 읽는다.
- **Services**: 그룹을 나눈다.
  - *Ready to use*: `usable`
  - *Available after connecting*: `needs_pairing` / `needs_space`. 동작은 "Connect device"뿐이며 Open chat은 없다.
  - *Device offline*: `peer_offline`. 접어서 표시한다.

## 6. 이행 계획

| 단계 | 내용 | 파일 | 위험 |
|---|---|---|---|
| P0 | 프런트만: `Ready/Open chat`을 `own ∨ binding_verified` 및 `health[peer] !== false`로 제한. 서비스만 있는 provider를 `discovered`로 표기. Home에 Discovered 줄 추가 | `webapp/src/personal/model.tsx`, `Pages.tsx`, `locales/personal.*.json` | 낮음 (API 변경 없음) |
| P1 ✅ | 백엔드 뷰모델: `discover()` 결과에 `relationship`/`presence`/`availability` 추가, `online_claim` 병기 (`online`은 호환 필드로 유지). 소비자 5곳을 `isServiceUsable`로 통일 | `rynmesh/llm_package/service_view.py`, `routes.py`, `api.py`, `peer_http.py`, `services/peer_health.py`, `webapp/src/domain/llmOrders.ts` | 중간 |
| P2 ✅ | `rynmesh/peer_directory.py`(순수 병합 계층)와 `GET /api/local/directory` 도입. capacity만 광고한 provider도 기기로 취급(`discoverySource: "capacity"`, heartbeat로 presence 판정). `FreshnessPolicy`로 TTL 단일화(기기 24h, 서비스 광고 1h, fresh 180s). `peers`/`peers/health`/`directory`가 같은 병합 결과를 읽음 | `rynmesh/peer_directory.py`, `peer_http.py`, `llm_package/routes.py`, `services/peer_health.py` | 중간~높음 |
| P3 ✅ | 프런트 단일 스토어: `PersonalProvider`가 `/api/local/directory` 스냅샷 하나만 읽음(없는 노드/fixture는 `directoryFromLegacy` 어댑터로 같은 형태 생성). directory 엔드포인트가 probe를 직접 수행해 `peersHealth` 별도 폴링 불필요. 화면 쪽 합성 기기 로직 제거 | `webapp/src/domain/directory.ts`, `webapp/src/personal/model.tsx`, `nodeClient.ts`, `liveNodeClient.ts`, `rynmesh/peer_http.py` | 중간 |

## 7. 수용 기준 (시나리오)

1. peer record만 있는 peer: 기기는 Discovered, 서비스 없음.
2. capacity 광고만 있는 peer: **기기가 Discovered로 존재**한다. 서비스는 고아가 되지 않는다.
3. `personal_space` 서비스, 비구성원: Home에 Discovered로 표시, Services는 `needs_space`, Open chat 없음.
4. 구성원 + 검증 완료: `usable`.
5. probe 실패인데 capacity가 `online_claim=true`: 서비스는 `peer_offline`.
6. TTL 초과 광고: 숨김 또는 `stale`.

테스트는 `tests/test_peer_directory.py`(백엔드)와 `webapp/src/personal/model.test.tsx`(프런트)에 둔다.

## 8. 열린 결정 (권장안)

- **D1** 인가되지 않은 발견 서비스를 보여 줄지: 보여 주되 별도 그룹에서 "Connect device" 동작만 허용 (권장).
- **D2** Home의 My devices 의미: 소유/구성원 전용 유지, Discovered는 별도 줄 (권장).
- **D3** registry 자동 게시 기본값: 현재 기본 on(`RYNMESH_AUTO_REGISTER`)이며 `peers/discover` 호출도 `register_node`를 실행한다. 별도 이슈로 opt-in 전환을 검토한다.

## 9. 범위 밖

- Personal Space 멤버십/서명 프로토콜 자체
- LLM dispatch 인가 로직(`routes.py:435`)은 변경하지 않는다. provider 쪽 최종 검사는 유지하고, 소비자 쪽 표시만 정합시킨다.
