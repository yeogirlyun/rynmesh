// The capability arrives only in the fragment; no analytics, referrer or query.
(() => {
  const message = document.getElementById('message');
  const notify = (text) => { message.textContent = text; message.focus(); };
  const field = document.getElementById('invite');
  const open = document.getElementById('open');
  let uri = '';
  try {
    uri = decodeURIComponent(location.hash.slice(1));
    if (!/^rynmesh:\/\/join\/[A-Za-z0-9_-]+$/.test(uri) || uri.length > 16384) throw Error();
    const encoded = uri.split('/').pop();
    const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))));
    const p = data.signed.payload;
    document.getElementById('identity').textContent = `Invitation from ${p.node_name}: ${p.ai?.service?.model_alias || 'Ryn friendship'}. Expires ${new Date(p.expires_at).toLocaleString()}. Identity will be verified in Ryn.`;
    field.value = uri;
    open.href = uri;
  } catch {
    uri = ''; open.removeAttribute('href');
    notify('The invitation is missing or invalid. Ask your friend to send the complete invitation again.');
  }
  document.getElementById('copy').addEventListener('click', async () => {
    try { if (!uri) throw Error(); await navigator.clipboard.writeText(uri); notify('Invitation copied. After installing, open Friends and paste it to continue.'); }
    catch { field.focus(); field.select(); notify('Automatic copy unavailable. Select the invitation and press Ctrl+C, then paste it in Ryn → Friends.'); field.focus(); field.select(); }
  });
  document.getElementById('clear').addEventListener('click', () => {
    uri = ''; field.value = ''; open.removeAttribute('href'); history.replaceState(null, '', location.pathname);
    notify('The invitation was cleared from this page. This does not revoke an invitation already accepted.');
  });
  fetch('/downloads/availability', {cache:'no-store'}).then(r => r.json()).then(data => {
    document.getElementById('availability').textContent = data.windows ? 'The Windows installer is available from this provider. Keep this page open during installation.' : 'This provider has no Windows installer available here. Ask them to send the Ryn Windows installer. Keep the invitation for later.';
    if (!data.windows) document.getElementById('download').removeAttribute('href');
  }).catch(() => { document.getElementById('availability').textContent = 'Installer availability could not be checked. Keep your invitation and retry when your friend is reachable.'; });
})();
