import { useEffect, useRef, useState } from "react";
import { sourceRequest, type AISource, type ModelShareScope } from "../domain/aiSources";
import { aiAccess, type AIGrant } from "../domain/aiAccess";
import { friendsApi } from "../domain/friendsClient";
import type { FriendRecord } from "../domain/friendTypes";
import { Modal } from "../personal/components";
import { useUILanguage } from "../uiI18n";

export default function ModelPoolSharing({ source, modelID, serviceID, onClose, onSaved }: { source: AISource; modelID: string; serviceID?: string; onClose: () => void; onSaved: () => Promise<unknown> }) {
  const zh = useUILanguage().startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [current, setCurrent] = useState(source);
  const actualScope = current.model_settings?.[modelID]?.effective.share_scope;
  const actualEnabled = current.shared_models?.includes(modelID) ?? current.sharing;
  const [scope, setScope] = useState<ModelShareScope>(actualScope || "personal_space");
  const [enabled, setEnabled] = useState(actualEnabled);
  const [cas, setCAS] = useState(false);
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [grants, setGrants] = useState<AIGrant[]>([]);
  const [errors, setErrors] = useState({ source: false, friends: false, grants: false });
  const [busy, setBusy] = useState("reload");
  const [reloadRequired, setReloadRequired] = useState(false);
  const [feedback, setFeedback] = useState("");
  const requestID = useRef(0);
  const lock = useRef(false);
  const mounted = useRef(true);
  const reload = async () => {
    const revision = ++requestID.current;
    setBusy("reload"); setFeedback("");
    const result = await Promise.allSettled([sourceRequest<{ sources: AISource[]; source_revision_cas_supported?: boolean }>(), friendsApi.list(), aiAccess.list()]);
    if (!mounted.current || revision !== requestID.current) return;
    const value = result[0].status === "fulfilled" ? result[0].value.sources.find(row => row.id === source.id) : undefined;
    setErrors({ source: !value, friends: result[1].status === "rejected", grants: result[2].status === "rejected" });
    if (value) { setCurrent(value); setScope(value.model_settings?.[modelID]?.effective.share_scope || "personal_space"); setEnabled(value.shared_models?.includes(modelID) ?? value.sharing); }
    setCAS(result[0].status === "fulfilled" && result[0].value.source_revision_cas_supported === true);
    if (result[1].status === "fulfilled") setFriends(result[1].value.friends.filter(friend => friend.status === "active"));
    if (result[2].status === "fulfilled") setGrants(result[2].value.grants);
    setReloadRequired(!value); setBusy("");
  };
  useEffect(() => { mounted.current = true; void reload(); return () => { mounted.current = false; requestID.current++; }; }, []);
  const mutate = async (kind: string, work: () => Promise<void>) => {
    if (lock.current || busy || reloadRequired) return;
    lock.current = true; setBusy(kind); setFeedback("");
    try { await work(); await onSaved(); }
    catch { if (mounted.current) { setReloadRequired(true); setFeedback(text("本次操作未完成确认，请重新读取实际状态后再选择。已完成的前一步不会自动撤销，其他步骤不会自动执行。", "This operation was not confirmed. Reload actual state before another choice. Earlier saved steps are not rolled back, and later steps do not run automatically.")); } }
    finally { lock.current = false; if (mounted.current) setBusy(""); }
  };
  const saveScope = () => void mutate("scope", async () => {
    const settings = current.model_settings?.[modelID];
    if (!settings) throw new Error("missing_model_settings");
    const next = await sourceRequest<AISource>(`/${encodeURIComponent(source.id)}/models/config`, { model_id: modelID, expected_rule_revision: settings.rule_revision, configuration: { share_scope: scope } }, "PUT");
    if (!mounted.current) return;
    setCurrent(next); setEnabled(next.shared_models?.includes(modelID) ?? next.sharing);
    setFeedback(text("使用范围已保存；分享启停与好友授权需分别确认，不会自动开启。", "Scope saved. Sharing and friend grants require separate confirmation; nothing is enabled automatically."));
  });
  const saveEnabled = () => void mutate("enabled", async () => {
    if (!cas || !current.source_revision) throw new Error("source_cas_unavailable");
    const selected = new Set(current.shared_models || (current.sharing ? current.models : []));
    if (enabled) selected.add(modelID); else selected.delete(modelID);
    const next = await sourceRequest<AISource>(`/${encodeURIComponent(source.id)}`, { shared_models: [...selected], expected_source_revision: current.source_revision }, "PUT");
    if (!mounted.current) return;
    setCurrent(next); setFeedback(text("此模型分享启停已保存，其他模型和默认模型不变。好友选择没有自动改变。", "Sharing saved for this model. Other models, the default model and friend choices remain unchanged."));
  });
  const setGrant = (friend: FriendRecord, allowed: boolean) => void mutate(friend.relationship_id, async () => {
    if (!serviceID) throw new Error("missing_service_id");
    const previous = grants.find(grant => grant.service_id === serviceID && grant.relationship_id === friend.relationship_id);
    const result = await aiAccess.set(serviceID, friend.relationship_id, allowed, previous?.revision || 0);
    if (!mounted.current) return;
    setGrants(rows => [...rows.filter(row => row.service_id !== serviceID || row.relationship_id !== friend.relationship_id), result.grant]);
    setFeedback(text("已保存这个好友对此模型的选择。撤权阻止后续请求，已经发送的数据不能收回。", "This friend's choice for this model is saved. Revocation blocks future requests; already-sent data cannot be recalled."));
  });
  const blocked = Boolean(busy) || reloadRequired || errors.source;
  return <Modal title={`${text("分享", "Share")} ${modelID}`} onClose={() => { if (!busy) onClose(); }}><div className="ais-modal mp-share-dialog mp-model-sharing">
    <p>{source.name} · {modelID}</p>
    <p className="ais-muted">{text("只改变此模型，助手默认与同来源其他模型保持独立。使用范围、分享启停和逐好友许可分步保存；一项成功不代表其他项完成。", "Only this model changes. Assistant defaults and sibling models stay independent. Scope, sharing and individual grants are saved separately; success in one step does not complete the others.")}</p>
    <label className="ais-field">{text("模型使用范围", "Model audience")}<select aria-label={text("模型使用范围", "Model audience")} value={scope} disabled={blocked} onChange={event => setScope(event.target.value as ModelShareScope)}><option value="personal_space">{text("个人空间", "Personal space")}</option><option value="friends">{text("指定好友", "Selected friends")}</option></select></label>
    <p>{actualScope === "friends" ? text("已保存范围：指定好友。仅已激活好友关系并获此模型许可的接收节点可调用，模型名不公开广播。", "Saved audience: selected friends. Only active, explicitly permitted receivers may call it; the model name is not publicly broadcast.") : text("已保存范围：个人空间。需要已加入个人空间且开启 AI 访问。好友许可不能绕过此范围。", "Saved audience: personal space. Membership and AI access are required. Friend grants cannot bypass this scope.")}</p>
    {actualEnabled && scope !== actualScope && <p>{text("先停用此模型分享再改变使用范围。新的范围保存后，核对好友许可并单独开启分享。", "Disable this model's sharing before changing its audience. After saving, review grants and enable sharing separately.")}</p>}
    <button className="ais-secondary" disabled={blocked || actualEnabled || scope === actualScope} onClick={saveScope}>{text("保存使用范围", "Save audience")}</button>
    <label className="ais-check"><input type="checkbox" checked={enabled} disabled={blocked} onChange={event => setEnabled(event.target.checked)} />{text("启用此模型对已保存范围的分享", "Enable sharing for this model's saved audience")}</label>
    <p className="ais-muted">{text("停用分享不会删除已记录的好友选择，再次开启时这些许可可能恢复生效。需要撤销某好友时，请单独点撤销。", "Disabling sharing keeps recorded friend choices. Re-enabling may make those grants effective again. Revoke an individual friend separately when needed.")}</p>
    {busy === "reload" ? <p role="status">{text("正在读取当前配置与好友许可…", "Loading current configuration and friend grants…")}</p> : !cas && <p role="status">{text("此节点尚未提供可核对版本的分享启停接口，请升级或重新读取。", "This node has not provided a version-checked sharing interface. Upgrade or reload.")}</p>}
    <button className="ais-secondary" disabled={blocked || !cas || !current.source_revision || enabled === actualEnabled || (enabled && scope !== actualScope)} onClick={saveEnabled}>{text("保存分享启停", "Save sharing switch")}</button>
    {(actualScope === "friends" || scope === "friends") && <section aria-label={text("逐好友模型许可", "Per-friend model permissions")}>
      <h3>{text("逐好友模型许可", "Per-friend model permissions")}</h3>
      {!serviceID ? <p>{text("模型目录没有提供稳定服务身份，暂不能修改好友许可。", "The catalog has not provided a stable service identity. Friend grants cannot be changed.")}</p> : busy === "reload" ? null : errors.friends || errors.grants ? <p role="alert">{text("好友或许可记录读取失败，不能据旧值修改。请重新读取。", "Could not load friends or grants. Reload before changing a previous value.")}</p> : !friends.length ? <p>{text("暂无已激活的好友。添加好友不会自动授权此模型。", "No active friends. Adding a friend does not grant model access.")}</p> : friends.map(friend => {
        const grant = grants.find(row => row.service_id === serviceID && row.relationship_id === friend.relationship_id);
        return <div className="mp-friend-grant" key={friend.relationship_id}><span><strong>{friend.node_name}</strong><small>{friend.peer_id}</small><small>{grant?.allowed ? actualScope === "friends" && actualEnabled && grant.effective ? text("已记录允许访问", "Access allowed") : text("已记录许可，当前未生效", "Grant recorded; not currently effective") : text("未获许可", "Not allowed")}</small></span><button className="ais-secondary" disabled={blocked || actualScope !== "friends"} onClick={() => setGrant(friend, !grant?.allowed)}>{grant?.allowed ? text("撤销此好友许可", "Revoke this friend") : text("允许此好友调用", "Allow this friend")}</button></div>;
      })}
    </section>}
    <p className="ais-muted">{text("对方只提交本次推理请求，提供方不附加本机助手会话、偏好、技能或工具，也不执行消费者工具。许可不等于网络可达或模型实际效果。", "The receiver submits one inference request. The provider adds no local assistant history, preferences, skills or tools, and does not execute consumer tools. Permission does not prove connectivity or model quality.")}</p>
    {errors.source && <p role="alert">{text("无法读取当前来源配置。", "Could not load current source configuration.")}</p>}
    {feedback && <p role={reloadRequired ? "alert" : "status"}>{feedback}</p>}
    <div className="ais-modal-footer"><small>{text("不影响助手默认模型", "Assistant default is independent")}</small><button className="ais-secondary" disabled={Boolean(busy)} onClick={() => void reload()}>{text("重新读取实际状态", "Reload actual state")}</button></div>
  </div></Modal>;
}
