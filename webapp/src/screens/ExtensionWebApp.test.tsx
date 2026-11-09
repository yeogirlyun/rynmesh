import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ExtensionWebApp from './ExtensionWebApp';
import { agentExtensions, queryPluginTool } from '../domain/agentExtensions';
import { invoke } from '@tauri-apps/api/core';
import { setLanguagePreference } from '../i18n';
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
vi.mock('../appContext', () => ({ useAppContext: () => ({ client: {} }) }));
vi.mock('../domain/agentExtensions', () => ({ agentExtensions: { overview: vi.fn() }, queryPluginTool: vi.fn() }));
vi.mock('../domain/peerProviders', () => ({ peerProviders: {} }));
const plugin = { id: 'reports', title: 'Reports', enabled: true, web_app: { url: 'https://app.example.test/', permissions: ['tools'] } };
beforeEach(() => { setLanguagePreference('en'); vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [plugin] } as any); vi.mocked(queryPluginTool).mockResolvedValue({ tool: 'read_rows', result: { count: 2 } }); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); vi.unstubAllGlobals(); delete (window as any).__TAURI_INTERNALS__; });
async function mount() {
  render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
  return await screen.findByTitle('Reports') as HTMLIFrameElement;
}
function request(frame: HTMLIFrameElement, overrides = {}) {
  fireEvent(window, new MessageEvent('message', { source: frame.contentWindow, origin: 'https://app.example.test', data: { type: 'ryn.plugin.request', id: 'one', method: 'tools.query', args: ['read_rows', {}] }, ...overrides }));
}
describe('remote app capability boundary', () => {
  it('automatically reopens after the managed service recovers', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockRejectedValueOnce('plugin_service_unhealthy').mockResolvedValue(true);
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Waiting for automatic recovery');
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(screen.getByTitle('Reports')).toBeInTheDocument();
    expect(vi.mocked(invoke).mock.calls.filter(call => call[0] === 'ensure_plugin_service')).toHaveLength(2);
  });
  it('explains a port conflict without mounting a foreign service', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockRejectedValue('plugin_service_port_in_use');
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Another process is using the plugin port');
    expect(screen.queryByTitle('Reports')).not.toBeInTheDocument();
  });
  it('waits for the managed desktop service before mounting the local frame', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    let ready!: (managed: boolean) => void;
    vi.mocked(invoke).mockImplementation((command: string) => command === 'ensure_plugin_service'
      ? new Promise(resolve => { ready = resolve; }) : Promise.resolve(101));
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    const pending = mount();
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('ensure_plugin_service', { pluginId: 'reports' }));
    expect(screen.queryByTitle('Reports')).not.toBeInTheDocument();
    ready(true);
    expect(await pending).toHaveAttribute('src', `http://127.0.0.1:18140/apps/reports?host_origin=${encodeURIComponent(window.location.origin)}`);
  });
  it('shows a stopped local service error and retries without leaving the plugin', async () => {
    const fetchService = vi.fn().mockRejectedValueOnce(new TypeError('connection refused')).mockResolvedValueOnce(new Response());
    vi.stubGlobal('fetch', fetchService);
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not connect');
    expect(screen.queryByTitle('Reports')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTitle('Reports')).toBeInTheDocument();
    expect(fetchService).toHaveBeenCalledTimes(2);
  });
  it('replaces an already loaded error document when the service restarts between polls', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    let instance = 101;
    vi.mocked(invoke).mockImplementation(async command => command === 'ensure_plugin_service' ? true : instance);
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    });
    const first = screen.getByTitle('Reports');
    // WebView2 fires load for chrome-error://chromewebdata, without an error event.
    fireEvent.load(first);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByTitle('Reports')).toBe(first);
    instance = 102;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByTitle('Reports')).not.toBe(first);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
  it('keeps an open page through slow health checks and only notes a sustained slowdown', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    let down = false;
    vi.mocked(invoke).mockImplementation(async command => {
      if (down && command === 'plugin_service_instance') throw 'plugin_service_unhealthy';
      return command === 'ensure_plugin_service' ? true : 101;
    });
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    });
    const first = screen.getByTitle('Reports');
    down = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(screen.getByTitle('Reports')).toBe(first);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole('status')).toHaveTextContent('responding slowly');
    expect(screen.getByTitle('Reports')).toBe(first);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    down = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByTitle('Reports')).toBe(first);
  });
  it('restarts an exited service from the open page and reloads only after the new process is up', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    let instance: number | 'exited' = 101;
    vi.mocked(invoke).mockImplementation(async command => {
      if (command === 'ensure_plugin_service') { if (instance === 'exited') instance = 102; return true; }
      if (instance === 'exited') throw 'plugin_service_exited';
      return instance;
    });
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }] } as any);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    });
    const first = screen.getByTitle('Reports');
    instance = 'exited';
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole('status')).toHaveTextContent('restarting');
    expect(screen.getByTitle('Reports')).toBe(first);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByTitle('Reports')).not.toBe(first);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
  it('keeps downloads blocked unless the plugin declares them', async () => {
    const frame=await mount();
    expect(frame.getAttribute('sandbox')?.split(' ')).not.toContain('allow-downloads');
    expect(frame.getAttribute('sandbox')?.split(' ')).not.toContain('allow-modals');
  });
  it('permits report downloads for a plugin that explicitly declares downloads', async () => {
    vi.mocked(agentExtensions.overview).mockResolvedValue({plugins:[{...plugin,web_app:{...plugin.web_app,permissions:['tools','downloads','dialogs']}}]} as any);
    const frame=await mount();
    expect(frame.getAttribute('sandbox')?.split(' ')).toContain('allow-downloads');
    expect(frame.getAttribute('sandbox')?.split(' ')).toContain('allow-modals');
    expect(frame.getAttribute('sandbox')?.split(' ')).not.toContain('allow-top-navigation');
  });
  it('calls only tools owned by the current enabled plugin', async () => {
    const frame=await mount(); const reply=vi.spyOn(frame.contentWindow!, 'postMessage'); request(frame);
    await waitFor(() => expect(reply).toHaveBeenCalled());
    expect(queryPluginTool).toHaveBeenCalledWith(plugin,'read_rows',{});
    expect(reply).toHaveBeenCalledWith({ type:'ryn.plugin.response',id:'one',result:{tool:'read_rows',result:{count:2}} },'https://app.example.test');
  });
  it('rejects an untrusted origin before dispatch', async () => {
    const frame=await mount(); request(frame,{origin:'https://untrusted.example'});
    expect(queryPluginTool).not.toHaveBeenCalled();
  });
  it('rechecks disabling before a request', async () => {
    const frame=await mount(); const reply=vi.spyOn(frame.contentWindow!, 'postMessage');
    vi.mocked(agentExtensions.overview).mockResolvedValue({plugins:[{...plugin,enabled:false}]} as any); request(frame);
    await waitFor(() => expect(reply).toHaveBeenCalled()); expect(queryPluginTool).not.toHaveBeenCalled();
  });
  it('rejects undeclared permissions', async () => {
    const frame=await mount(); const reply=vi.spyOn(frame.contentWindow!, 'postMessage');
    vi.mocked(agentExtensions.overview).mockResolvedValue({plugins:[{...plugin,web_app:{...plugin.web_app,permissions:[]}}]} as any); request(frame);
    await waitFor(() => expect(reply).toHaveBeenCalled()); expect(queryPluginTool).not.toHaveBeenCalled();
  });
});
