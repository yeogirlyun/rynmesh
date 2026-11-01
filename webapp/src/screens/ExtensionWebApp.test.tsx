import { fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ExtensionWebApp from './ExtensionWebApp';
import { agentExtensions, queryPluginTool } from '../domain/agentExtensions';
vi.mock('../appContext', () => ({ useAppContext: () => ({ client: {} }) }));
vi.mock('../domain/agentExtensions', () => ({ agentExtensions: { overview: vi.fn() }, queryPluginTool: vi.fn() }));
vi.mock('../domain/peerProviders', () => ({ peerProviders: {} }));
const plugin = { id: 'reports', title: 'Reports', enabled: true, web_app: { url: 'https://app.example.test/', permissions: ['tools'] } };
beforeEach(() => { vi.mocked(agentExtensions.overview).mockResolvedValue({ plugins: [plugin] } as any); vi.mocked(queryPluginTool).mockResolvedValue({ tool: 'read_rows', result: { count: 2 } }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
async function mount() {
  render(<MemoryRouter initialEntries={['/apps/extensions/reports']}><Routes><Route path='/apps/extensions/:pluginId' element={<ExtensionWebApp />} /></Routes></MemoryRouter>);
  return await screen.findByTitle('Reports') as HTMLIFrameElement;
}
function request(frame: HTMLIFrameElement, overrides = {}) {
  fireEvent(window, new MessageEvent('message', { source: frame.contentWindow, origin: 'https://app.example.test', data: { type: 'ryn.plugin.request', id: 'one', method: 'tools.query', args: ['read_rows', {}] }, ...overrides }));
}
describe('remote app capability boundary', () => {
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
