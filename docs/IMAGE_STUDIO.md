# Image studio plugin

The image application is maintained independently at `D:/code/ryn-image-studio`
(local repository only; no remote publication). Its README documents building,
installation, rollback and storage compatibility.

Ryn does not ship or auto-register this application. It loads explicitly installed
node-site plugins; see [NODE_SITES.md](NODE_SITES.md).
The existing site ID `image-studio`, owner bookmark `/apps/sites/image-studio`,
`<node home>/image-studio/studio.db` and image files are preserved by the plugin.
Install the plugin before upgrading an existing image-studio host. Other devices
opening its shared page do not need to install the plugin.
