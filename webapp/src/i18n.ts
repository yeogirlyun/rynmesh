import i18n from "i18next";
import { invoke } from "@tauri-apps/api/core";
import { initReactI18next } from "react-i18next";
import personalEn from "./locales/personal.en.json";
import personalZh from "./locales/personal.zh-CN.json";
import uiEn from "./locales/ui.en.json";
import uiZh from "./locales/ui.zh-CN.json";

export type LanguagePreference = "system" | "zh-CN" | "en";
const STORAGE_KEY = "ryn.language";

export function readLanguagePreference(): LanguagePreference {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "zh-CN" || value === "en" ? value : "system";
  } catch {
    return "system";
  }
}

export function resolveLanguage(preference: LanguagePreference, systemLanguage = navigator.language): "zh-CN" | "en" {
  if (preference !== "system") return preference;
  const language = systemLanguage.toLowerCase();
  if (/^zh(?:$|-)/.test(language)) return "zh-CN";
  return "en";
}

const en = {
  personal: personalEn,
  nav: {
    home: "Home", services: "Services", devices: "Devices", tasks: "Tasks", nas: "NAS",
    more: "More", explore: "Explore", messages: "Messages", settings: "Settings",
    library: "Library", forYou: "For you", searchAsk: "Search & ask", publish: "Publish",
    myServices: "My services", browseServices: "Browse services", serviceSetup: "Service setup",
    apiAccess: "API access", myDevices: "My devices", connectionDetails: "Connection details",
    preferences: "Preferences", desktop: "Desktop", personalSpace: "Personal space",
    plugins: "Plugins", advancedSettings: "Advanced settings", aiChat: "AI chat",
  },
  shell: {
    mainNavigation: "Main navigation", primary: "Primary", manageSpace: "Manage personal space",
    personal: "Personal", preview: "Design preview · Sample data", closeNavigation: "Close navigation",
    toggleNavigation: "Toggle navigation", search: "Search devices or services",
    searchSpace: "Search your space", searchInput: "Search devices and services",
    searchPlaceholder: "Find a device or service…", toggleAppearance: "Toggle light and dark appearance",
    device: "Device", service: "Service", noMatches: "No matches",
    noResults: "Try another device or service name.",
  },
  settings: {
    subtitle: "These preferences apply to this device.",
    general: "General", language: "App language", languageHelp: "Only affects this device.",
    system: "Follow system", chinese: "简体中文", english: "English",
    personalSpaceHelp: "Add your computers, manage membership and share AI.",
    manageSpace: "Manage space", appearance: "Appearance", appearanceHelp: "Choose the app’s appearance.",
    light: "Light", dark: "Dark", desktopHelp: "Startup, system tray, availability and recovery.",
    desktopSettings: "Desktop settings", connection: "Connection", connected: "Connected",
    disconnected: "Disconnected", status: "Status: {{status}}",
    connectionHelp: "Show connection problems and retry options.", diagnostics: "Connection diagnostics",
    dataPrivacy: "Data & privacy", privateNotes: "Private notes", privateNotesHelp: "Visible only to you.",
    localHistory: "Local history", localHistoryHelp: "Manage records stored on this device.",
    manage: "Manage", thisDevice: "This device", localService: "Local service", running: "Running",
    stopped: "Stopped", discoveryConnection: "Discovery connection", knownDevices: "Known devices",
    checkAgain: "Check again", checkComplete: "Connection check complete",
  },
  pages: {
    homeSubtitle: "Use your devices. Share what they can do.",
    servicesSubtitle: "Use capabilities from your devices and devices shared with you.",
    devicesSubtitle: "Manage your devices and devices shared with you.",
    tasksSubtitle: "Track work running across your devices.",
  },
} as const;

const zh = {
  personal: personalZh,
  nav: {
    home: "首页", services: "服务", devices: "设备", tasks: "任务", nas: "NAS",
    more: "更多", explore: "探索", messages: "消息", settings: "设置",
    library: "资料库", forYou: "为你推荐", searchAsk: "搜索与提问", publish: "发布",
    myServices: "我的服务", browseServices: "浏览服务", serviceSetup: "服务设置",
    apiAccess: "API 接入", myDevices: "我的设备", connectionDetails: "连接详情",
    preferences: "偏好设置", desktop: "桌面端", personalSpace: "个人空间",
    plugins: "插件", advancedSettings: "高级设置", aiChat: "AI 对话",
  },
  shell: {
    mainNavigation: "主导航", primary: "主导航", manageSpace: "管理个人空间",
    personal: "个人空间", preview: "设计预览 · 示例数据", closeNavigation: "关闭导航",
    toggleNavigation: "切换导航", search: "搜索设备或服务",
    searchSpace: "搜索个人空间", searchInput: "搜索设备和服务",
    searchPlaceholder: "查找设备或服务…", toggleAppearance: "切换浅色或深色外观",
    device: "设备", service: "服务", noMatches: "没有匹配项",
    noResults: "试试其他设备或服务名称。",
  },
  settings: {
    subtitle: "这些偏好设置仅适用于此设备。",
    general: "通用", language: "界面语言", languageHelp: "仅影响这台设备。",
    system: "跟随系统", chinese: "简体中文", english: "English",
    personalSpaceHelp: "添加电脑、管理成员并共享 AI 服务。",
    manageSpace: "管理空间", appearance: "外观", appearanceHelp: "选择应用的外观模式。",
    light: "浅色", dark: "深色", desktopHelp: "启动、系统托盘、在线状态与恢复。",
    desktopSettings: "桌面端设置", connection: "连接", connected: "已连接",
    disconnected: "未连接", status: "状态：{{status}}",
    connectionHelp: "查看连接问题和重试选项。", diagnostics: "连接诊断",
    dataPrivacy: "数据与隐私", privateNotes: "私人备注", privateNotesHelp: "仅你自己可见。",
    localHistory: "本地历史记录", localHistoryHelp: "管理保存在此设备上的记录。",
    manage: "管理", thisDevice: "此设备", localService: "本地服务", running: "运行中",
    stopped: "已停止", discoveryConnection: "发现服务连接", knownDevices: "已知设备",
    checkAgain: "重新检查", checkComplete: "连接检查完成",
  },
  pages: {
    homeSubtitle: "使用你的设备，分享它们的能力。",
    servicesSubtitle: "使用自己设备和他人共享的服务。",
    devicesSubtitle: "管理你的设备和他人共享的设备。",
    tasksSubtitle: "查看各设备上运行的任务。",
  },
};

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en, ui: uiEn }, "zh-CN": { translation: zh, ui: uiZh } },
  lng: resolveLanguage(readLanguagePreference()),
  fallbackLng: "en",
  supportedLngs: ["en", "zh-CN"],
  interpolation: { escapeValue: false },
  initAsync: false,
});

export function setLanguagePreference(preference: LanguagePreference): void {
  try {
    localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    // The choice still applies until this session ends.
  }
  const language = resolveLanguage(preference);
  void i18n.changeLanguage(language);
  document.documentElement.lang = language;
  window.dispatchEvent(new Event("ryn-language-preference"));
}

export function refreshSystemLanguage(): void {
  if (readLanguagePreference() === "system") {
    void i18n.changeLanguage(resolveLanguage("system"));
    document.documentElement.lang = i18n.language;
  }
}

// The tray and native confirmations share the webview's resolved language.
async function syncDesktopLanguage(): Promise<void> {
  if (!("__TAURI_INTERNALS__" in window)) return;
  try {
    await invoke("set_desktop_language", { language: i18n.resolvedLanguage || i18n.language });
  } catch {
    // Older desktop shells may not expose this command; web localization still works.
  }
}
i18n.on("languageChanged", () => { void syncDesktopLanguage(); });
void syncDesktopLanguage();
document.documentElement.lang = i18n.language;
window.addEventListener("languagechange", refreshSystemLanguage);

export default i18n;
