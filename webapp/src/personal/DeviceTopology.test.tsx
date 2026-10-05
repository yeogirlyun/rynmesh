import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeviceTopology } from "./DeviceTopology";
import { networkFromEndpoint, networkFromPeer } from "./deviceNetwork";
import type { Device } from "./model";

const self: Device = { id: "self", name: "My laptop", note: "", kind: "laptop", own: true, self: true, online: true };
const remote: Device = { id: "remote", name: "Office PC", note: "", kind: "desktop", own: true, online: null };

describe("network evidence", () => {
  it("groups a freshly discovered Mac with this desktop while a registry-only desktop remains unconfirmed", () => {
    const mac = networkFromPeer("http://172.19.0.1:8791", ["http://172.16.8.117:8791"], true);
    const remotePc = networkFromPeer("http://192.168.31.65:8791", []);
    expect(mac).toMatchObject({ scope: "local", route: "lan", address: "172.16.8.117" });
    expect(remotePc).toMatchObject({ scope: "unknown", route: "unknown" });
    render(<DeviceTopology devices={[self,
      { ...remote, id: "mac", name: "Mac", online: true, network: mac },
      { ...remote, id: "public-pc", name: "Public PC", online: true, network: remotePc },
    ]} onSelect={vi.fn()} onEdit={vi.fn()} onShare={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Current network" })).toContainElement(screen.getByRole("button", { name: "Mac, Online" }));
    expect(screen.getByRole("region", { name: "Network unconfirmed" })).toContainElement(screen.getByRole("button", { name: "Public PC, Online" }));
  });
  it.each(["http://192.168.1.5:8792", "http://10.0.0.3", "http://172.16.0.1", "http://100.64.0.1", "http://127.0.0.1", "http://[::1]", "http://[fd00::1]", "https://pc.example.com", "http://203.0.113.24", "not a URL", ""])("does not infer LAN membership or an active route from %s", endpoint => {
    expect(networkFromEndpoint(endpoint)).toMatchObject({ scope: "unknown", route: "unknown" });
  });
  it("distinguishes an advertised public address from a verified route and strips credentials", () => {
    expect(networkFromEndpoint("https://user:secret@8.8.8.8:8792/path?token=secret")).toEqual({
      id: "public", label: "Public endpoints", scope: "public", route: "unknown", address: "8.8.8.8",
    });
  });
});

it("shows the local device without pretending it connects to itself", () => {
  const { container } = render(<DeviceTopology devices={[self]} selected={self} onSelect={vi.fn()} onEdit={vi.fn()} onShare={vi.fn()} />);
  expect(screen.getByRole("button", { name: "My laptop, This device, Online" })).toBeInTheDocument();
  expect(container.querySelector(".pf-topology-hub")).toBeNull();
  expect(container.querySelectorAll(".pf-topology-wires path")).toHaveLength(0);
});

it("keeps unknown membership honest and preserves selection/edit/share actions", () => {
  const onSelect = vi.fn(), onEdit = vi.fn(), onShare = vi.fn();
  render(<DeviceTopology devices={[self, remote]} selected={remote} onSelect={onSelect} onEdit={onEdit} onShare={onShare} />);
  expect(screen.getByText("Network unconfirmed")).toBeInTheDocument();
  expect(screen.getByText("Status unknown · route unconfirmed")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Office PC, Status unknown" }));
  expect(onSelect).toHaveBeenCalledWith("remote");
  fireEvent.click(screen.getByRole("button", { name: "Edit Office PC" }));
  expect(onEdit).toHaveBeenCalledWith(remote);
  fireEvent.click(screen.getByRole("button", { name: "Share service" }));
  expect(onShare).toHaveBeenCalledOnce();
});

it("never claims an offline device currently has a direct connection", () => {
  const offline: Device = { ...remote, online: false, network: { id: "office", label: "Office network", scope: "remote", route: "public" } };
  render(<DeviceTopology devices={[self, offline]} selected={offline} onSelect={vi.fn()} onEdit={vi.fn()} onShare={vi.fn()} />);
  expect(screen.getByText("Offline · last known network")).toBeInTheDocument();
  expect(screen.queryByText("Public direct")).not.toBeInTheDocument();
});

it("labels a VPN discovery address separately from online status and connection route", () => {
  const online = { ...remote, online: true, network: networkFromEndpoint("http://10.10.10.200:8791") };
  render(<DeviceTopology devices={[self, online]} selected={online} onSelect={vi.fn()} onEdit={vi.fn()} onShare={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Office PC, Online" })).toBeInTheDocument();
  expect(screen.getByText("Advertised address: 10.10.10.200 (actual connection may use another address)")).toBeInTheDocument();
  expect(screen.getByText("Route unconfirmed")).toBeInTheDocument();
  expect(screen.queryByText("Offline · last known network")).not.toBeInTheDocument();
});
