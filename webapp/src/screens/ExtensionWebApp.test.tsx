import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ExtensionWebApp from './ExtensionWebApp';
import { agentExtensions, bootstrapCompanyBrowserSession, queryPluginTool } from '../domain/agentExtensions';
import { invoke } from '@tauri-apps/api/core';
import { setLanguagePreference } from '../i18n';
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn() }));
vi.mock('../appContext', () => ({ useAppContext: () => ({ client: {} }) }));
vi.mock('../domain/agentExtensions', () => ({ agentExtensions: { overview: vi.fn() }, bootstrapCompanyBrowserSession: vi.fn(), queryPluginTool: vi.fn() }));
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
  it('requires a trusted app-ready message, keeps a timed-out frame, and restores its route on recovery', async () => {
    const company = { ...plugin, id: 'company-dashboard', web_app: { url: 'https://company.example.test/apps/company-dashboard', permissions: [] } };
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [company] } as any);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    await act(async () => {
      render(<MemoryRouter initialEntries={['/apps/extensions/company-dashboard']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    });
    const first = screen.getByTitle('Reports') as HTMLIFrameElement;
    const authorizationReply = vi.spyOn(first.contentWindow!, 'postMessage');
    fireEvent.load(first);
    expect(first).toHaveAttribute('data-page-state', 'loading');
    const lifecycle = (origin: string, phase: string, route?: string) => fireEvent(window, new MessageEvent('message', {
      source: first.contentWindow, origin, data: { type: 'ryn.plugin.lifecycle', phase, route },
    }));
    lifecycle('https://foreign.example', 'ready');
    expect(first).toHaveAttribute('data-page-state', 'loading');
    authorizationReply.mockClear();
    fireEvent(window, new MessageEvent('message', { source: first.contentWindow, origin: 'https://foreign.example', data: { type: 'ryn.company.authorization.changed' } }));
    expect(authorizationReply).not.toHaveBeenCalled();
    fireEvent(window, new MessageEvent('message', { source: first.contentWindow, origin: 'https://company.example.test', data: { type: 'ryn.company.authorization.changed' } }));
    expect(authorizationReply).toHaveBeenCalledWith({ type: 'ryn.company.authorization.changed' }, 'https://company.example.test');
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByTitle('Reports')).toBe(first);
    expect(screen.getByRole('alert')).toHaveTextContent('has not finished initializing');
    lifecycle('https://company.example.test', 'ready', '/apps/company-dashboard?view=cash-flow');
    expect(first).toHaveAttribute('data-page-state', 'ready');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    lifecycle('https://company.example.test', 'error', 'https://foreign.example/');
    expect(screen.getByTitle('Reports')).toBe(first);
    fireEvent.click(screen.getByRole('button', { name: 'Reload page' }));
    expect(screen.getByTitle('Reports')).not.toBe(first);
    expect(screen.getByTitle('Reports').getAttribute('src')).toContain('/apps/company-dashboard?view=cash-flow&host_origin=');
  });
  it('delivers a one-use browser bootstrap only to an enabled company frame', async () => {
    vi.mocked(bootstrapCompanyBrowserSession).mockResolvedValue({ ticket: 'one-use-test-ticket', origin: 'https://app.example.test' });
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, id: 'company-workspace' }] } as any);
    render(<MemoryRouter initialEntries={['/apps/extensions/company-workspace']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    const frame = await screen.findByTitle('Reports') as HTMLIFrameElement;
    const reply = vi.spyOn(frame.contentWindow!, 'postMessage');
    request(frame, { data: { type: 'ryn.plugin.request', id: 'bootstrap', method: 'company.session.bootstrap', args: [] } });
    await waitFor(() => expect(bootstrapCompanyBrowserSession).toHaveBeenCalledWith('company-workspace'));
    expect(reply).toHaveBeenCalledWith({ type: 'ryn.plugin.response', id: 'bootstrap', result: { ticket: 'one-use-test-ticket', origin: 'https://app.example.test' } }, { targetOrigin: 'https://app.example.test' });
  });
  it('never passes a company ticket to a manifest with a mismatched service origin', async () => {
    vi.mocked(bootstrapCompanyBrowserSession).mockResolvedValue({ ticket: 'must-not-leak', origin: 'http://127.0.0.1:18140' });
    vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [{ ...plugin, id: 'company-workspace' }] } as any);
    render(<MemoryRouter initialEntries={['/apps/extensions/company-workspace']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    const frame = await screen.findByTitle('Reports') as HTMLIFrameElement;
    const reply = vi.spyOn(frame.contentWindow!, 'postMessage');
    request(frame, { data: { type: 'ryn.plugin.request', id: 'bootstrap', method: 'company.session.bootstrap', args: [] } });
    await waitFor(() => expect(reply).toHaveBeenCalled());
    expect(JSON.stringify(reply.mock.calls)).not.toContain('must-not-leak');
  });
  it('does not offer company bootstrap to another plugin', async () => {
    const frame = await mount();
    const reply = vi.spyOn(frame.contentWindow!, 'postMessage');
    request(frame, { data: { type: 'ryn.plugin.request', id: 'bootstrap', method: 'company.session.bootstrap', args: [] } });
    await waitFor(() => expect(reply).toHaveBeenCalled());
    expect(bootstrapCompanyBrowserSession).not.toHaveBeenCalled();
  });
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
  it('replaces the plugin frame with an updating notice while its service pack switches and reopens automatically when done', async () => {
    (window as any).__TAURI_INTERNALS__ = {};
    vi.mocked(invoke).mockImplementation(async command => command === 'ensure_plugin_service' ? true : 201);
    vi.mocked(agentExtensions.overview).mockResolvedValue({
      plugins: [{ ...plugin, web_app: { ...plugin.web_app, url: 'http://127.0.0.1:18140/apps/reports' } }],
    } as any);
    render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
    const first = await screen.findByTitle('Reports');
    await act(async () => {
      window.dispatchEvent(new CustomEvent('ryn:service-update-changed', {
        detail: {
          services: [{
            id: 'suite',
            name: { 'zh-CN': '套件插件', en: 'Suite plugins' },
            plugins: ['reports'],
            plugin_names: ['Reports'],
            state: 'applying',
            current_version: '2026.11.12',
            available_version: '2026.11.12',
            downloaded: 0,
            total: null,
          }],
        },
      }));
    });
    expect(screen.queryByTitle('Reports')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Updating Suite plugins to 2026.11.12…');
    await act(async () => {
      window.dispatchEvent(new CustomEvent('ryn:service-update-changed', {
        detail: {
          services: [{
            id: 'suite',
            name: { 'zh-CN': '套件插件', en: 'Suite plugins' },
            plugins: ['reports'],
            plugin_names: ['Reports'],
            state: 'up_to_date',
            current_version: '2026.11.12',
            available_version: null,
            downloaded: 0,
            total: null,
          }],
        },
      }));
    });
    const reopened = await screen.findByTitle('Reports');
    expect(reopened).toBeInTheDocument();
    expect(reopened).not.toBe(first);
  });
  it('rejects undeclared permissions', async () => {
    const frame=await mount(); const reply=vi.spyOn(frame.contentWindow!, 'postMessage');
    vi.mocked(agentExtensions.overview).mockResolvedValue({plugins:[{...plugin,web_app:{...plugin.web_app,permissions:[]}}]} as any); request(frame);
    await waitFor(() => expect(reply).toHaveBeenCalled()); expect(queryPluginTool).not.toHaveBeenCalled();
  });
});
