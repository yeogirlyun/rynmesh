# 浼佷笟寰俊宸ヤ綔鍖烘巿鏉冭瘖鏂笌淇锛?026-11-05锛?
鏈枃浠跺彧璁板綍闃舵銆佸彈鎺ч敊璇爜銆佺鍙ｃ€佹椂闂村拰缁熻鏁板瓧锛屼笉鍖呭惈鎺堟潈鐮併€乻tate銆丳KCE銆佷护鐗屻€佸洖璋冨畬鏁?URL銆佽亰澶╂鏂囨垨鑱旂郴浜鸿韩浠姐€?
## 涓€銆佽瘉鎹潵婧?
| 鏉ユ簮 | 鍐呭 |
|---|---|
| 涓荤▼搴忔棩蹇楋紙瀹為檯鍐欏叆浣嶇疆涓?Codex 搴旂敤闅旂鐩綍 `Packages/OpenAI.Codex_*/LocalCache/Local/Ryn/logs`锛屼笉鏄櫘閫?`%LOCALAPPDATA%/Ryn/logs`锛?| `api_ext_*` 澶辫触闃舵銆佽繘绋?PID 涓庨噸鍚椂闂?|
| 涓荤▼搴忔墿灞曠姸鎬佹帴鍙?| `company-workspace-mcp` 鐨勬巿鏉冪姸鎬併€佽祫婧?URL銆佸彂鐜板伐鍏锋暟 |
| 涓績 hk036 chat-backend 鏁版嵁搴擄紙鍙浜嬪姟锛?| `ryn_auth_request` / `ryn_auth_code` / `ryn_audit_event` 鐨勬椂闂淬€佽寖鍥淬€佺姸鎬併€佸洖鐜鍙ｃ€佸鎴风绫诲瀷 |
| 鏈満 Chrome 鍘嗗彶锛堜粎璇诲彇鏃堕棿銆佹潵婧愩€佽矾寰勫拰鍙傛暟鍚嶏級 | 鎺堟潈璺宠浆閾?|
| 涓婁竴浼氳瘽鎿嶄綔璁板綍 | 浣曟椂銆佺敱璋佸彂璧风櫥褰?|

## 浜屻€佹椂闂寸嚎锛堥娓椂闂达級

| 鏃堕棿 | 浜嬩欢 | 璇佹嵁 |
|---|---|---|
| 12:21:44 | 涓婁竴浼氳瘽鐩存帴璋冪敤涓荤▼搴?`oauth/login`锛屽彇寰楀師濮嬫巿鏉?URL锛屽洖鐜鍙?61993 | 浼氳瘽璁板綍銆丆hrome 鍘嗗彶 |
| 12:22:09 | 鑷姩 Chrome 鏍囩鎵撳紑鎺堟潈 URL锛涗腑蹇冨綋鏃跺皻鏈儴缃?`workspace.access`锛岀珛鍗冲洖璺?`127.0.0.1:61993/callback?error=invalid_target` | Chrome 鍘嗗彶鍙傛暟鍊?`error=invalid_target` |
| 12:22:09 | 涓荤▼搴忔妸浠讳綍 `error` 閮借褰曚负 `agent_mcp_oauth_login_denied`锛屽苟浣滀负宸ヤ綔鍖鸿鐨勬寔涔呴敊璇繚瀛?| 婧愮爜 `complete_login`锛?2:35:36 蹇収宸叉槸璇ラ敊璇?|
| 12:35:15 | 缁忕敤鎴锋壒鍑嗭紝涓績閮ㄧ讲 `workspace.access`锛坮ynConfig.js銆乺yn.controller.js锛?| 涓婁竴浼氳瘽璁板綍 |
| 12:35:43 | 绗簩娆＄洿鎺ヨ皟鐢?`oauth/login`锛屽洖鐜鍙?65213锛屾湰鏈烘祦绋?600 绉掑悗鍒版湡锛堢害 12:45:47锛?| 浼氳瘽璁板綍 |
| 12:35:56 | 鑷姩 Chrome 鎵撳紑鎺堟潈 URL 鈫?浼佷笟寰俊鐧诲綍椤碉紱涔嬪悗娴忚鍣ㄨ嚜鍔ㄥ寲琚紒涓氬井淇＄珯鐐瑰畨鍏ㄧ瓥鐣ユ嫤鎴?| 涓績 `login_requested`锛孋hrome 鍘嗗彶 |
| 12:44:32 | 涓績鏀跺埌娴忚鍣紙Mozilla UA锛変紒涓氬井淇″洖璋冿紝韬唤璇嗗埆鎴愬姛锛岀鍙?60 绉掓巿鏉冪爜锛屽洖璺崇洰鏍囦负 `127.0.0.1:65213/callback` | 涓績 `ryn_auth_code.expires_at=12:45:32`锛岃姹?`completed` |
| 12:44:32 涔嬪悗 | 鏈満浠庢湭鎵ц浠ょ墝浜ゆ崲锛氭巿鏉冪爜 `used_at` 涓虹┖銆佷腑蹇冩棤 `workspace.access` grant銆佹棤 `code_reuse`/`grant_created` 瀹¤锛涙湰鏈哄伐浣滃尯琛岄敊璇粛涓?12:22 鐨勬棫鍊?| 涓績鍙鏌ヨ銆佷富绋嬪簭鐘舵€?|
| 12:45:47 | 鏈満娴佺▼鍒版湡锛屽洖鐜洃鍚叧闂紱鐘舵€侀噸鏂版樉绀?12:22 鐨勬棫閿欒 | 婧愮爜 `_expire` |
| 12:49 / 13:53 | 涓荤▼搴忎袱娆￠噸鍚紙PID 38548 鈫?鏂拌繘绋嬶級锛屽唴瀛樹腑鐨勬巿鏉冩祦绋嬩笉鍙仮澶?| 涓荤▼搴忔棩蹇?|
| 14:24:53鈥?4:25:12 | 鐢ㄦ埛缁忔彃浠跺唴缃祦绋嬪畬鎴愪竴娆?`finance.read` 鐧诲綍锛坧ython-httpx 瀹㈡埛绔級锛実rant 鍒涘缓鎴愬姛锛涜鐧诲綍涓嶅惈宸ヤ綔鍖鸿祫婧?| 涓績瀹¤ |

## 涓夈€佺粨璁?
### 宸茶瘉瀹?
1. `agent_mcp_oauth_login_denied` 涓嶆槸鐢ㄦ埛鎷掔粷锛屼篃涓嶆槸 12:35 閭ｆ鎵爜鐨勭粨鏋溿€傚畠鏉ヨ嚜 12:22 涓績杩斿洖鐨?`invalid_target`锛堜腑蹇冨綋鏃朵笉鏀寔璇ヨ祫婧愶級锛屼富绋嬪簭鎶婃墍鏈夋巿鏉冪閿欒缁熶竴鏄犲皠涓?`login_denied`銆?2. 涔嬪悗姣忔鏂扮櫥褰曢兘涓嶆竻闄よ繖涓棫閿欒锛涙祦绋嬪埌鏈熸垨鍙栨秷鍚庯紝鐘舵€佸張鍥炲埌杩欎釜鏃ч敊璇€傛墍浠?12:35 閭ｆ鐪熷疄鎵爜澶辫触鍚庯紝鐣岄潰鍜屾帴鍙ｄ粛鏄剧ず 12:22 鐨勫師鍥狅紝璇鎺掓煡銆?3. 12:35:56 閭ｆ鎺堟潈涓紝涓績浜?12:44:32 绛惧彂浜嗘巿鏉冪爜锛屼絾瀹冩病鏈夎鏈満浠ょ墝绔偣娑堣垂锛氭湰鏈烘病鏈変氦鎹㈣褰曪紝涓績娌℃湁 grant锛屼篃娌℃湁浠ょ墝绔偣鎷掔粷鐨勭棔杩广€?4. 鎺堟潈澶辫触鍚庡伐浣滃尯鏈嶅姟鍣ㄦ病鏈変护鐗岋紝鎵€浠ュ彂鐜板伐鍏锋暟涓?0锛宍tool-query` 杩斿洖 `agent_mcp_tool_disabled`銆傝繖鏄巿鏉冪己澶辩殑涓嬫父缁撴灉锛屼笉鏄亰澶╁簱涓虹┖銆?5. 鍏徃鐧诲綍锛圖ashboard 鍏ュ彛锛夊彧鍙栧緱 `finance.read`銆傚伐浣滃尯璧勬簮鏄湰鏈?`http://127.0.0.1:18140/ryn/agent-mcp`锛屼笌 `/ryn/mcp` 涓嶆槸鍚屼竴璧勬簮锛屼笉鑳藉叡浜护鐗岋紱蹇呴』鍗曠嫭绛惧彂 `workspace.access`銆傚洜姝も€滃叕鍙歌处鍙峰凡鐧诲綍鈥濅笉绛変簬宸ヤ綔鍖哄凡鎺堟潈銆?6. 涓荤▼搴忔棩蹇楀啓鍏?Codex 闅旂鐩綍锛沗McpOAuthError` 鐨勫彈鎺ч敊璇爜杩囧幓涓嶈繘鏃ュ織锛堝彧鏈夌被鍨嬪悕锛夛紱鍥炵幆鍥炶皟銆佷护鐗屼氦鎹㈠拰鎺堟潈鍚庡彂鐜伴兘娌℃湁浠讳綍闃舵璁板綍銆?
### 鏈兘鍞竴纭畾

12:44:32 鐨勫洖璺充负浣曟病鏈夊埌杈炬湰鏈哄洖鐜€侰hrome 鍘嗗彶涓紝浠ュ線鎴愬姛鐧诲綍锛?0-23銆?0-26锛夐兘鍚屾椂璁板綍浜嗏€滀腑蹇冨洖璋冣€濆拰鈥滄湰鏈哄洖鐜€濅袱璺筹紱杩欐涓よ烦閮芥病鏈夋彁浜ゅ埌鍘嗗彶锛岃€屼腑蹇冪‘瀹炰互娴忚鍣?UA 澶勭悊浜嗗洖璋冦€傚彲鑳芥槸鑷姩鍖栨祻瑙堝櫒鏍囩鍦ㄧ珯鐐瑰畨鍏ㄧ瓥鐣ユ嫤鎴悗琚帶鍒舵垨涓銆傜幇鏈夋湰鏈鸿瘉鎹笉鑳藉尯鍒嗭紝鏃х増鏈富绋嬪簭涔熸病鏈夊洖鐜眰鏃ュ織銆傛柊鐗堟湰宸茶ˉ榻愯繖閮ㄥ垎闃舵鏃ュ織锛岃嫢鍐嶆鍙戠敓鍙洿鎺ュ垽瀹氥€?
### 涓轰粈涔堜笉鑳芥仮澶嶇幇鏈夋巿鏉?
鎺堟潈鐮佹湁鏁堟湡 60 绉掞紝宸蹭簬 12:45:32 杩囨湡锛涙湰鏈?PKCE verifier 鍜?state 鍙瓨鍦ㄤ簬 12:35:43 鍚姩鐨勮繘绋嬪唴瀛樹腑锛岃娴佺▼宸蹭簬 12:45:47 鍒版湡锛岃繘绋嬩篃宸查噸鍚€傜幇鏈?`finance.read` 浠ょ墝鐨勮祫婧愬拰鑼冨洿閮戒笉鍚岋紝涓嶈兘鍐掑厖 `workspace.access`銆傚畨鍏ㄧ殑鍋氭硶鍙湁閲嶆柊绛惧彂涓€娆″伐浣滃尯鎺堟潈銆?
## 鍥涖€佷慨澶嶏紙涓荤▼搴?`codex/workspace-oauth-diagnostics-20261105`锛?
| 缂洪櫡 | 淇 |
|---|---|
| 鎺堟潈绔敊璇粺涓€璁颁负 `login_denied` | 鎸?RFC 6749/8707 鏄犲皠锛歚access_denied`鈫抈login_denied`锛沗invalid_target`鈫抈agent_mcp_oauth_invalid_target`锛沗invalid_scope`鈫抈agent_mcp_oauth_invalid_scope`锛沗invalid_request` 绛夆啋`agent_mcp_oauth_authorization_rejected`锛沗server_error`/`temporarily_unavailable`鈫抈agent_mcp_oauth_authorization_unavailable`锛涙湭鐭ュ€间粛璁颁负 `login_denied` |
| 鏃ч敊璇湪鏂扮櫥褰曚腑娈嬬暀 | 鏂扮櫥褰曞紑濮嬫椂锛岃嫢璇ヨ娌℃湁浠ょ墝锛屾竻闄や笂娆″凡缁撴潫娴佺▼鐣欎笅鐨勯敊璇紱鎸佹湁浠ょ墝鐨勮淇濈暀閿欒锛堥敊璇粛闃绘杩欎簺鍑嵁琚娇鐢級銆傚埌鏈熴€佸彇娑堝洖鍒?`logged_out`锛屼笉鍐嶆樉绀烘棫鍘熷洜 |
| 鍥炵幆銆佷氦鎹€佸彂鐜伴樁娈垫棤鏃ュ織 | 鏂板 `agent_oauth` 闃舵鏃ュ織锛歚login_started`锛堝惈鍥炵幆绔彛锛屽彲涓庝腑蹇?`redirect_uri` 绔彛瀵圭収锛夈€乣callback_rejected`锛堝師鍥狅細`host`銆乣proxy_or_origin_header`銆乣duplicate_param`銆乣state_mismatch`銆乣issuer_mismatch`銆乣flow_expired`銆乣flow_not_waiting` 绛夛級銆乣callback_accepted`銆乣authorization_error`锛堝彈鎺т笂娓搁敊璇灇涓撅級銆乣token_exchange_started/failed`锛圚TTP 鐘舵€併€佸彈鎺?OAuth 閿欒锛夈€乣login_connected/failed/expired/cancelled`銆乣discovery_finished`锛堟垚鍔熶笌鍚︺€佸伐鍏锋暟锛夈€傚叧鑱旀爣璇嗘槸鎺堟潈娴佺▼ ID 鐨勫崟鍚?12 浣嶆憳瑕?|
| 澶辫触鏃ュ織涓㈠け OAuth 閿欒鐮?| `agent_failure` 璁板綍 `McpOAuthError` 鐨勫彈鎺?`agent_*` 鐮?|

鏃ュ織鍑芥暟鍙帴鍙楁灇涓俱€佹湇鍔″櫒 ID銆佺鍙ｃ€佽鏁板拰鍗曞悜鎽樿锛屾棤娉曞啓鍏?URL銆乧ode銆乻tate銆乿erifier 鎴?token銆傝韩浠姐€佽祫婧愩€佹潈闄愯寖鍥淬€乮ssuer銆乻tate 涓?PKCE 鏍￠獙鍧囨湭鏀惧銆?
### 娴嬭瘯

- 鏂板 `tests/test_mcp_oauth_incident_replay.py`锛? 椤癸級锛岀敤鏈湴鍚堟垚鎺堟潈鏈嶅姟閲嶆斁鏈浜嬫晠锛歚invalid_target` 姝ｇ‘涓婃姤涓斾笉姹℃煋涓嬩竴娆＄櫥褰曪紱涓嬩竴娆＄櫥褰曟棤鍥炶皟鑰屽埌鏈熷悗鐘舵€佷负 `logged_out`锛涚涓夋鐧诲綍鎴愬姛骞跺畬鎴愬彂鐜帮紱杩熷埌鎴栧鏉ュ洖璋冭褰曞師鍥犱笖涓嶅彂浠ょ墝璇锋眰锛涗护鐗岀鐐规嫆缁濊褰?HTTP 400 鍜?`invalid_grant`锛涙棩蹇椾腑涓嶅嚭鐜颁换浣?code銆乻tate 鎴?auth_id銆?- 鍘?OAuth 鐩稿叧娴嬭瘯 127 椤瑰叏閮ㄩ€氳繃锛屽悎璁?132 椤广€?- 鎵╁睍銆佸叕鍙搞€佽瘖鏂€丮CP 鐩稿叧鍥炲綊瑙佷笅鏂囨墽琛岃褰曪紱`test_extension_data_policy_runtime::test_automatic_compaction_retains_policy_and_rejects_disallowed_summary_destination` 鍦ㄦ湭淇敼鐨?develop `56abed0` 涓婂悓鏍峰け璐ワ紙`agent_tool_schema_limit`锛夛紝涓庢湰淇鏃犲叧銆?
## 浜斻€佷粛闇€澶勭悊

1. 宸ヤ綔鍖烘巿鏉冮渶瑕佹湰浜洪噸鏂扮‘璁や竴娆★紙瑙佺涓夎妭鈥滀负浠€涔堜笉鑳芥仮澶嶁€濓級銆傚繀椤诲湪 Ryn 宸ヤ綔鍙板唴鐨勪紒涓氬井淇℃巿鏉冨叆鍙ｅ畬鎴愶紱涓嶈浣跨敤鑷姩鍖栨祻瑙堝櫒鎵撳紑鍘熷鎺堟潈 URL銆?2. `company-workspace-mcp` 褰撳墠 `read_only_tools` 鏄棭鏈熼仐鐣欑殑璐㈠姟宸ュ叿鍚嶏紝涓嶅寘鍚换浣曚紒涓氬井淇″伐鍏枫€傛彃浠舵洿鏂颁笉浼氳鐩栧睘涓昏缃紝鎵€浠ョ湡瀹?agent 浼氭妸浼佷笟寰俊璇诲彇宸ュ叿鎸夆€滃啓鍏?楂橀闄┾€濆鐞嗭紝姣忔闇€瑕佸鎵广€俙tool-query` 楠屾敹鍙緷璧栧伐鍏疯嚜韬殑 `readOnlyHint`锛屼笉鍙楀奖鍝嶃€傛槸鍚︽妸浼佷笟寰俊鍙宸ュ叿璁句负鍏嶅鎵癸紝灞炰簬灞炰富鐨勫畨鍏ㄥ喅瀹氾紝椤荤粡鐢ㄦ埛纭銆?3. 鎻掍欢澹版槑鐨?`read_only_tools` 鏈垪鍏?`wecom_history_status` 鍜?`wecom_read_message`锛堝彧褰卞搷鍏ㄦ柊瀹夎锛夈€?