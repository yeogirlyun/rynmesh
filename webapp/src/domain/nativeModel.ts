import { nodeControlUrl } from "./nodeUrl";
import { tr } from "../uiI18n";

export interface NativeModelStatus {
  id: string; name: string; file_name: string; size_bytes: number; peer_id: string;
  state: "not_installed" | "installing" | "starting" | "running" | "stopping" | "stopped" | "error";
  stage: string; progress: number | null; error: string; backend: string;
  installed: boolean; supported: boolean; autostart: boolean; sharing: boolean; active_requests: number;
}
export interface LocalModelFile { name: string; file_name: string; size_bytes: number; architecture?: string }
export async function inspectNativeModel(source: string): Promise<LocalModelFile> {
  const response = await fetch(nodeControlUrl("/llm/native/inspect"), {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "operation_failed");
  return result;
}
export function uploadNativeModel(file: File, progress: (value: number) => void, signal: AbortSignal): Promise<NativeModelStatus> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", nodeControlUrl(`/llm/native/upload?filename=${encodeURIComponent(file.name)}`));
    request.withCredentials = true;
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
    const abort = () => request.abort();
    signal.addEventListener("abort", abort, { once: true });
    request.onloadend = () => signal.removeEventListener("abort", abort);
    request.onload = () => {
      try {
        const result = JSON.parse(request.responseText);
        if (request.status >= 200 && request.status < 300) resolve(result);
        else reject(new Error(result.detail || "operation_failed"));
      } catch { reject(new Error("operation_failed")); }
    };
    request.onerror = () => reject(new Error("operation_failed"));
    request.onabort = () => reject(new Error("cancelled"));
    request.send(file);
  });
}
export async function nativeModelRequest(action = "", body?: unknown, method = "POST"): Promise<NativeModelStatus> {
  const response = await fetch(nodeControlUrl(`/llm/native${action ? `/${action}` : ""}`), {
    credentials: "include", ...(body !== undefined ? { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "operation_failed");
  return result;
}
export function nativeError(code: string): string {
  const messages: Record<string, string> = {
    unsupported_platform: "一键安装暂支持 Apple 芯片 Mac 和 Windows x64。可以连接已有服务。",
    insufficient_memory: "可用内存不足，请关闭其他程序或选择更小的模型。",
    insufficient_disk: "安装空间不足，请清理磁盘后重试。",
    unsupported_file: "请选择单个 .gguf 模型文件。",
    unsupported_model: "暂不支持这个模型架构。请使用兼容的 Qwen、Llama、Gemma 或 Phi 文本模型。",
    invalid_gguf: "模型文件无效或不完整，请检查原文件。",
    model_too_large: "单个模型文件不能超过 64 GB。",
    split_model_unsupported: "暂不支持分片模型，请选择完整的单个 GGUF 文件。",
    unsupported_quantization: "暂不支持这个量化类型，请选择常规 Q4、Q5、Q6、Q8 或浮点 GGUF 文件。",
    model_changed: "模型文件内容已改变，请重新导入。",
    model_already_installed: "请先卸载当前模型，再导入其他模型。原文件会保留。",
    download_checksum_invalid: "文件校验失败，请重新下载。",
    download_incomplete: "下载未完成，点击重试继续下载。",
    model_busy: "模型正在处理操作，请稍候。",
    startup_timeout: "模型启动超时，请释放内存后重试。",
    runtime_exited: "模型未能正常运行，请检查设备兼容性后重试。",
    runtime_missing: "运行环境不完整，请重试安装。",
    personal_space_required: "请先加入个人空间，并在空间设置中启用 AI 访问。",
    self_test_failed: "模型回答测试失败，请重试。",
  };
  return tr(messages[code] || "操作未完成，请检查网络和可用空间后重试。");
}
export function nativeState(status: NativeModelStatus): string {
  const states = { not_installed: "尚未安装", installing: "正在安装", starting: "正在启动", running: "正在运行", stopping: "正在停止", stopped: "已停止", error: "需要处理" };
  return tr(states[status.state]);
}
export const nativeBusy = (status: NativeModelStatus) => ["installing", "starting", "stopping"].includes(status.state);
