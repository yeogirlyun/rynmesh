import { act, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CompanyAuthorizationNotice } from './CompanyAuthorizationNotice';
import { observeCompanyAuthorizationDelivery } from '../domain/companyAuthorizationDelivery';
import { setLanguagePreference } from '../i18n';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
it('does not claim completion until a durable service ACK and ignores stale responses', async () => {
  setLanguagePreference('en');
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  const fetchStatus = vi.fn().mockResolvedValue(new Response(JSON.stringify({ sequence: 100, acknowledged: true, pending: false })));
  vi.stubGlobal('fetch', fetchStatus);
  render(<CompanyAuthorizationNotice />);
  act(() => observeCompanyAuthorizationDelivery({ sequence: 100, acknowledged: false, pending: true }));
  expect(screen.getByRole('status')).toHaveTextContent('the change is not yet fully applied');
  act(() => observeCompanyAuthorizationDelivery({ sequence: 99, acknowledged: true, pending: false }));
  expect(screen.getByRole('status')).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(fetchStatus).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  act(() => observeCompanyAuthorizationDelivery({ sequence: 100, acknowledged: false, pending: true }));
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
