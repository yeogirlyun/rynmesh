import { tr } from "../uiI18n";
import {
  isPermissionGranted,
  onAction,
  registerActionTypes,
  requestPermission,
  sendNotification,
  type Options,
} from "@tauri-apps/plugin-notification";
import type { NodeSettings } from "./types";

const LAST_SENT_KEY = "rynmesh.notification.lastSent";
const ACTION_TYPE = "rynmesh-discovery";

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function isQuietHour(settings: NodeSettings, date = new Date()): boolean {
  const hour = date.getHours();
  const start = settings.notification_quiet_start;
  const end = settings.notification_quiet_end;
  if (start === end) return false;
  return start < end ? hour >= start && hour < end : hour >= start || hour < end;
}

function minimumInterval(settings: NodeSettings): number {
  if (settings.notification_frequency === "daily") return 24 * 60 * 60 * 1000;
  if (settings.notification_frequency === "hourly") return 60 * 60 * 1000;
  return 0;
}

export function notificationDue(settings: NodeSettings, now = Date.now()): boolean {
  if (!settings.notifications_enabled || isQuietHour(settings, new Date(now))) return false;
  const previous = Number(window.localStorage.getItem(LAST_SENT_KEY) || 0);
  return now - previous >= minimumInterval(settings);
}

export async function requestDesktopNotificationPermission(): Promise<NotificationPermission | "unsupported"> {
  if (isTauri()) {
    if (await isPermissionGranted()) return "granted";
    return requestPermission();
  }
  if (!("Notification" in window)) return "unsupported";
  if (window.Notification.permission === "granted") return "granted";
  const permission = await window.Notification.requestPermission();
  return permission;
}

export async function installNotificationNavigation(onOpen: () => void): Promise<() => void> {
  if (!isTauri()) return () => undefined;
  await registerActionTypes([
    {
      id: ACTION_TYPE,
      actions: [{ id: "open-for-you", title: tr("Open For You"), foreground: true }],
    },
  ]);
  const listener = await onAction((notification: Options) => {
    if (notification.actionTypeId === ACTION_TYPE || notification.extra?.route === "/digest") {
      onOpen();
    }
  });
  return () => listener.unregister();
}

export async function sendDiscoveryNotification(
  settings: NodeSettings,
  unreadCount: number,
  onOpen: () => void,
): Promise<boolean> {
  if (unreadCount <= 0 || !notificationDue(settings)) return false;
  const permission = await requestDesktopNotificationPermission();
  if (permission !== "granted") return false;
  const title = tr("Ryn found something for you");
  const body = tr("{{count}} new recommendations ready.", { count: unreadCount });
  if (isTauri()) {
    sendNotification({
      title,
      body,
      actionTypeId: ACTION_TYPE,
      autoCancel: true,
      group: "rynmesh-discovery",
      extra: { route: "/digest" },
    });
  } else {
    const notification = new window.Notification(title, { body, tag: "rynmesh-discovery" });
    notification.onclick = () => {
      window.focus();
      onOpen();
      notification.close();
    };
  }
  window.localStorage.setItem(LAST_SENT_KEY, String(Date.now()));
  return true;
}

export async function sendTestNotification(onOpen: () => void): Promise<boolean> {
  const permission = await requestDesktopNotificationPermission();
  if (permission !== "granted") return false;
  if (isTauri()) {
    sendNotification({
      title: tr("Ryn notifications are ready"),
      body: tr("Future recommendations can bring you directly back to For You."),
      actionTypeId: ACTION_TYPE,
      autoCancel: true,
      extra: { route: "/digest" },
    });
  } else {
    const notification = new window.Notification(tr("Ryn notifications are ready"), {
      body: tr("Future recommendations can bring you directly back to For You."),
    });
    notification.onclick = onOpen;
  }
  return true;
}
